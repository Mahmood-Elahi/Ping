"""Fingerprint company recruiting platforms from official careers evidence.

The script is intentionally read-only with respect to the live company registry.
It fetches official URLs sequentially, processes at most 25 companies per persisted
batch, preserves reviewed fingerprints, and emits implementation-planning reports.
"""

from __future__ import annotations

import argparse
import csv
import html as html_module
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

try:
    from scripts.company_watchlist_common import (
        _safe_public_https_target,
        canonicalize_url,
        clean_text,
        normalize_company_name,
        parse_bool,
        write_csv,
    )
    from scripts.enrich_company_watchlist import read_workbook
except ModuleNotFoundError:
    from company_watchlist_common import (  # type: ignore[no-redef]
        _safe_public_https_target,
        canonicalize_url,
        clean_text,
        normalize_company_name,
        parse_bool,
        write_csv,
    )
    from enrich_company_watchlist import read_workbook  # type: ignore[no-redef]


DEFAULT_INPUT = Path("config/company_watchlist.xlsx")
DEFAULT_RESEARCH = Path("config/company_watchlist_research.csv")
DEFAULT_OUTPUT = Path("config/company_platform_fingerprints.csv")
PLATFORM_SUMMARY = Path("output/platform_family_summary.csv")
FINGERPRINT_DETAILS = Path("output/platform_fingerprint_details.csv")
MANUAL_REVIEW = Path("output/platform_manual_review.csv")
ADAPTER_PRIORITY = Path("output/adapter_priority_report.csv")
PARENT_SOURCES = Path("output/parent_company_sources.csv")
MAX_BATCH_SIZE = 25
MAX_HTML_BYTES = 3_000_000
READY_RESEARCH_STATUSES = {"existing_config", "validated_supported"}
CONFIDENCE_RANK = {"": 0, "low": 1, "medium": 2, "high": 3}

FINGERPRINT_FIELDS = [
    "Company",
    "Official Careers URL",
    "Official Job Search URL",
    "Current Research Status",
    "Detected Platform Family",
    "Vendor",
    "API or Endpoint URL",
    "Endpoint Method",
    "Required Parameters",
    "Pagination Method",
    "Stable Job ID Available",
    "Apply URL Available",
    "JavaScript Required",
    "Authentication Required",
    "Anti-Bot Observed",
    "Parent Company Source",
    "Evidence URL",
    "Evidence Type",
    "Confidence",
    "Reviewed",
    "Adapter Recommendation",
    "Notes",
]

PARENT_SOURCE_RULES: dict[str, dict[str, str]] = {
    "googledeepmind": {
        "parent": "Google",
        "parameters": "q=Google DeepMind; validate organization on every result",
        "notes": "Official DeepMind careers routes to Google Careers; free-text filtering creates duplicate-source risk.",
    },
    "ubereats": {
        "parent": "Uber",
        "parameters": "team=UberEats",
        "notes": "Official Uber team filter shares the Uber source; deduplicate by the existing Uber source key.",
    },
    "threads": {
        "parent": "Meta",
        "parameters": "q=Threads; validate team/product on every result",
        "notes": "Meta Careers is the shared source; text search is not a guaranteed organization filter.",
    },
    "whatsapp": {
        "parent": "Meta",
        "parameters": "teams[0]=WhatsApp",
        "notes": "Meta Careers exposes a stable team parameter; deduplicate against the Meta source.",
    },
    "instagram": {
        "parent": "Meta",
        "parameters": "teams[0]=Instagram",
        "notes": "Meta Careers exposes a stable team parameter; deduplicate against the Meta source.",
    },
    "twitch": {
        "parent": "Amazon",
        "parameters": "team/content route=Twitch; validate team on every result",
        "notes": "Official Twitch careers routes to Amazon Jobs; deduplicate against the Amazon source.",
    },
    "wechat": {
        "parent": "Tencent",
        "parameters": "organization/product=WeChat; safe structured filter not yet evidenced",
        "notes": "WeChat uses the Tencent careers source; keep result-level validation because the supplied URL is unfiltered.",
    },
    "universalrobots": {
        "parent": "Teradyne",
        "parameters": "site/path filter=Universal-Robots",
        "notes": "The official brand page links to the Teradyne job source; deduplicate against parent listings.",
    },
    "imperialoil": {
        "parent": "ExxonMobil",
        "parameters": "site/path filter=Imperial",
        "notes": "The official company page links to the ExxonMobil job family with an Imperial path filter.",
    },
    "walmartglobaltech": {
        "parent": "Walmart",
        "parameters": "career-area=technology; validate organization on every result",
        "notes": "The technology brand uses Walmart Careers; deduplicate against any Walmart source.",
    },
    "targettech": {
        "parent": "Target",
        "parameters": "career-area=technology",
        "notes": "The technology brand uses Target's shared jobs source; deduplicate against any Target source.",
    },
    "costcotechnology": {
        "parent": "Costco",
        "parameters": "LOCATION_ID=99418745224; validate technology category on every result",
        "notes": "Costco Technology uses the parent careers source and an external location identifier.",
    },
    "htcvive": {
        "parent": "HTC",
        "parameters": "product/team=VIVE; safe structured filter not yet evidenced",
        "notes": "VIVE uses HTC's regional careers source; product-level filtering remains review-gated.",
    },
    "ocadotechnology": {
        "parent": "Ocado Group",
        "parameters": "career-area=technology",
        "notes": "Ocado Technology uses the group careers source; deduplicate against group listings.",
    },
}


@dataclass(frozen=True)
class FetchEvidence:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    text: str
    note: str
    headers: Mapping[str, str]


@dataclass(frozen=True)
class PlatformDetection:
    family: str
    vendor: str
    evidence_type: str
    evidence_marker: str
    confidence: str
    endpoint_url: str = ""
    endpoint_method: str = ""
    required_parameters: str = ""
    pagination_method: str = ""
    stable_job_id: str = "unknown"
    apply_url: str = "unknown"
    javascript_required: str = "unknown"
    authentication_required: str = "false"
    anti_bot: str = "false"


class ControlledFingerprintClient:
    """Sequential HTTP client with explicit timeouts, pacing, and retries."""

    def __init__(
        self,
        *,
        delay: float = 0.5,
        retries: int = 1,
        timeout: int = 15,
        session: Optional[requests.Session] = None,
        sleep_fn: Any = time.sleep,
    ) -> None:
        if delay < 0:
            raise ValueError("delay cannot be negative")
        if retries < 0 or retries > 3:
            raise ValueError("retries must be between 0 and 3")
        if timeout < 1 or timeout > 60:
            raise ValueError("timeout must be between 1 and 60 seconds")
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.session = session or requests.Session()
        self.sleep_fn = sleep_fn
        self._request_count = 0

    def fetch(self, url: str) -> FetchEvidence:
        current = canonicalize_url(url)
        requested = current
        for redirect_count in range(6):
            safe, note = _safe_public_https_target(current)
            if not safe:
                return FetchEvidence(requested, current, 0, "", "", note, {})
            response = self._request(current)
            try:
                status = int(response.status_code)
                if status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location", "")
                    if not location:
                        return FetchEvidence(requested, current, status, "", "", "Redirect without Location", dict(response.headers))
                    if redirect_count == 5:
                        return FetchEvidence(requested, current, status, "", "", "Too many redirects", dict(response.headers))
                    current = canonicalize_url(urljoin(current, location))
                    continue
                content_type = response.headers.get("Content-Type", "")
                raw = response.raw.read(MAX_HTML_BYTES + 1, decode_content=True)
                if len(raw) > MAX_HTML_BYTES:
                    return FetchEvidence(requested, current, status, content_type, "", "Response exceeded 3 MB", dict(response.headers))
                text = raw.decode(response.encoding or "utf-8", errors="replace")
                return FetchEvidence(
                    requested,
                    current,
                    status,
                    content_type,
                    text,
                    f"HTTP {status}",
                    dict(response.headers),
                )
            finally:
                response.close()
        return FetchEvidence(requested, current, 0, "", "", "Too many redirects", {})

    def _request(self, url: str) -> requests.Response:
        last_error: Optional[requests.RequestException] = None
        for attempt in range(self.retries + 1):
            if self._request_count:
                self.sleep_fn(self.delay)
            self._request_count += 1
            try:
                response = self.session.get(
                    url,
                    allow_redirects=False,
                    timeout=self.timeout,
                    stream=True,
                    headers={
                        "User-Agent": "JobRadarPlatformFingerprint/1.0",
                        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                    },
                )
                if response.status_code in {429, 500, 502, 503, 504} and attempt < self.retries:
                    response.close()
                    continue
                return response
            except requests.RequestException as exc:
                last_error = exc
        assert last_error is not None
        raise last_error


def _host_matches(host: str, domain: str) -> bool:
    host = host.casefold().rstrip(".")
    domain = domain.casefold().rstrip(".")
    return host == domain or host.endswith("." + domain)


def _marker(text: str, patterns: Iterable[str]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0)[:160]
    return ""


def _absolute_endpoint(base_url: str, value: str) -> str:
    cleaned = html_module.unescape(value).replace("\\/", "/").strip("\\\"'()")
    return canonicalize_url(urljoin(base_url, cleaned))


def _extract_endpoint(base_url: str, text: str, patterns: Iterable[str]) -> str:
    normalized = html_module.unescape(text).replace("\\/", "/")
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return _absolute_endpoint(base_url, match.group(0))
    return ""


def _workday_detection(url: str) -> Optional[PlatformDetection]:
    parsed = urlsplit(url)
    match = re.fullmatch(r"(?P<tenant>[a-z0-9-]+)\.(?P<subdomain>wd\d+)\.myworkdayjobs\.com", (parsed.hostname or "").casefold())
    if not match:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if parts and re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", parts[0]):
        parts = parts[1:]
    site = parts[0] if parts else ""
    base = f"https://{match.group('tenant')}.{match.group('subdomain')}.myworkdayjobs.com"
    endpoint = f"{base}/wday/cxs/{match.group('tenant')}/{site}/jobs" if site else ""
    return PlatformDetection(
        "workday", "Workday", "official ATS URL", f"host={(parsed.hostname or '')}; site={site or 'missing'}",
        "high" if site else "medium", endpoint, "POST", "limit; offset; searchText; appliedFacets",
        "offset + limit", "true", "true", "false", "false", "false",
    )


def detect_platform(evidence: FetchEvidence) -> PlatformDetection:
    """Classify a fetched official page using auditable URL/content signatures."""
    url = evidence.final_url or evidence.requested_url
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path
    decoded = html_module.unescape(evidence.text).replace("\\/", "/")
    searchable = f"{url}\n{decoded}"

    workday = _workday_detection(url)
    if workday:
        return workday

    url_rules = [
        ("greenhouse", "Greenhouse", ("boards.greenhouse.io", "job-boards.greenhouse.io"), "GET", "board slug", "none", "true", "true"),
        ("lever", "Lever", ("jobs.lever.co",), "GET", "account slug; mode=json", "none", "true", "true"),
        ("ashby", "Ashby", ("jobs.ashbyhq.com",), "GET", "job board name", "none", "true", "true"),
        ("smartrecruiters", "SmartRecruiters", ("jobs.smartrecruiters.com",), "GET", "company identifier; limit; offset", "offset + limit", "true", "true"),
        ("workable", "Workable", ("apply.workable.com",), "GET", "account slug", "page or token", "true", "true"),
        ("rippling", "Rippling", ("ats.rippling.com",), "GET", "company slug", "unknown", "true", "true"),
        ("eightfold", "Eightfold AI", ("eightfold.ai",), "POST", "domain; start; num", "start + num", "true", "true"),
        ("icims", "iCIMS", ("icims.com",), "GET", "search filters; pr/page", "page", "true", "true"),
        ("jobvite", "Jobvite", ("jobs.jobvite.com",), "GET", "company identifier", "page", "true", "true"),
        ("avature", "Avature", ("avature.net",), "GET", "portal-specific filters", "page", "true", "true"),
        ("brassring", "BrassRing", ("brassring.com",), "GET", "partnerid; siteid", "page", "true", "true"),
        ("bamboohr", "BambooHR", ("bamboohr.com",), "GET", "company subdomain", "none", "true", "true"),
        ("recruitee", "Recruitee", ("recruitee.com",), "GET", "company subdomain", "none", "true", "true"),
        ("teamtailor", "Teamtailor", ("teamtailor.com",), "GET", "company subdomain", "page", "true", "true"),
        ("deel", "Deel", ("jobs.deel.com",), "GET", "tenant slug", "bounded ItemList", "true", "true"),
        ("personio", "Personio", ("jobs.personio.com", "jobs.personio.de"), "GET", "company identifier", "page", "true", "true"),
    ]
    for family, vendor, domains, method, params, pagination, stable_id, apply_url in url_rules:
        matched_domain = next((domain for domain in domains if _host_matches(host, domain)), "")
        if not matched_domain:
            continue
        endpoint = url
        if family == "greenhouse":
            slug = next((part for part in path.split("/") if part), "")
            endpoint = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        elif family == "lever":
            slug = next((part for part in path.split("/") if part), "")
            endpoint = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        elif family == "ashby":
            slug = next((part for part in path.split("/") if part), "")
            endpoint = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        elif family == "smartrecruiters":
            slug = next((part for part in path.split("/") if part), "")
            endpoint = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
        return PlatformDetection(
            family, vendor, "official ATS URL", f"host matched {matched_domain}", "high", endpoint,
            method, params, pagination, stable_id, apply_url, "false", "false", "false",
        )

    oracle_host = bool(re.search(r"\.fa\.[a-z0-9-]+\.oraclecloud\.com$", host))
    oracle_marker = _marker(searchable, [r"CandidateExperience", r"recruitingCEJobRequisitions", r"hcmUI/CandidateExperience"])
    if oracle_host or oracle_marker:
        endpoint = _extract_endpoint(url, decoded, [r"https://[^\"'\s<>]+/hcmRestApi/resources/[^\"'\s<>]+", r"/hcmRestApi/resources/(?:latest|\d+\.\d+\.\d+)/recruitingCEJobRequisitions"])
        if not endpoint and oracle_host:
            endpoint = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        return PlatformDetection(
            "oracle_recruiting", "Oracle Recruiting Cloud", "official ATS URL" if oracle_host else "page source marker",
            f"host={host}" if oracle_host else f"marker={oracle_marker}", "high", endpoint, "GET",
            "finder=ByRecruitingSite; siteNumber; limit; offset", "offset + limit", "true", "true",
            "true", "false", "false",
        )

    taleo_marker = _marker(searchable, [r"taleo\.net", r"oracle\.taleo", r"careersection/[a-z0-9_-]+/jobsearch\.ftl"])
    if taleo_marker:
        endpoint = _extract_endpoint(url, decoded, [r"https://[^\"'\s<>]*taleo\.net/[^\"'\s<>]+", r"/careersection/[^\"'\s<>]+/jobsearch\.ftl"])
        return PlatformDetection(
            "oracle_taleo", "Oracle Taleo", "page source marker", f"marker={taleo_marker}", "high",
            endpoint or url, "GET/POST", "career section; search filters", "page", "true", "true", "true", "false", "false",
        )

    content_rules = [
        ("sap_successfactors", "SAP SuccessFactors", [r"successfactors\.(?:com|eu)", r"careerSiteCompanyId", r"SAP SuccessFactors"], [r"https://[^\"'\s<>]*successfactors\.(?:com|eu)/(?:career|sfcareer)[^\"'\s<>]*"], "GET/POST", "company; career site; search filters", "page or offset", "true", "true"),
        ("icims", "iCIMS", [r"careers-[^\"'\s<>]+\.icims\.com", r"jobs\.icims\.com", r"iCIMS"], [r"https://[^\"'\s<>]*icims\.com/jobs/[^\"'\s<>]*"], "GET", "search filters; pr/page", "page", "true", "true"),
        ("phenom", "Phenom", [r"phenompeople\.com", r"Phenom(?: People)?", r"phApp\."], [r"https://[^\"'\s<>]+/api/(?:jobs|jobsearch|search)[^\"'\s<>]*", r"/api/(?:jobs|jobsearch|search)[^\"'\s<>]*"], "GET/POST", "keyword; location; page/offset; family-specific facets", "page or offset", "true", "true"),
        ("eightfold", "Eightfold AI", [r"eightfold\.ai", r"/api/pcsx/"], [r"https://[^\"'\s<>]+/api/pcsx/(?:search|jobs)[^\"'\s<>]*", r"/api/pcsx/(?:search|jobs)[^\"'\s<>]*"], "POST", "domain; start; num; filters", "start + num", "true", "true"),
        ("smartrecruiters", "SmartRecruiters", [r"api\.smartrecruiters\.com", r"jobs\.smartrecruiters\.com"], [r"https://api\.smartrecruiters\.com/v1/companies/[^\"'\s<>]+/postings"], "GET", "company identifier; limit; offset", "offset + limit", "true", "true"),
        ("jobvite", "Jobvite", [r"jobs\.jobvite\.com", r"Jobvite"], [r"https://jobs\.jobvite\.com/[^\"'\s<>]+"], "GET", "company identifier", "page", "true", "true"),
        ("avature", "Avature", [r"avature\.net", r"Avature"], [r"https://[^\"'\s<>]*avature\.net/[^\"'\s<>]+"], "GET", "portal-specific filters", "page", "true", "true"),
        ("brassring", "BrassRing", [r"brassring\.com", r"Kenexa BrassRing"], [r"https://[^\"'\s<>]*brassring\.com/[^\"'\s<>]+"], "GET", "partnerid; siteid", "page", "true", "true"),
        ("dayforce", "Dayforce", [r"dayforcehcm\.com", r"Dayforce Jobs"], [r"https://[^\"'\s<>]*dayforcehcm\.com/[^\"'\s<>]+"], "GET", "client namespace; page", "page", "true", "true"),
        ("adp", "ADP Recruiting", [r"workforcenow\.adp\.com", r"recruiting\.adp\.com"], [r"https://[^\"'\s<>]*adp\.com/[^\"'\s<>]+"], "GET/POST", "client/account identifiers", "page or offset", "true", "true"),
    ]
    for family, vendor, patterns, endpoint_patterns, method, params, pagination, stable_id, apply_url in content_rules:
        marker = _marker(searchable, patterns)
        if marker:
            endpoint = _extract_endpoint(url, decoded, endpoint_patterns) or url
            return PlatformDetection(
                family, vendor, "page source marker", f"marker={marker}", "high", endpoint, method,
                params, pagination, stable_id, apply_url, "true", "false", "false",
            )

    anti_bot_marker = _marker(
        f"{decoded}\n{json.dumps(dict(evidence.headers))}",
        [r"cf-chl-[a-z-]+", r"challenge-platform", r"Attention Required! \| Cloudflare", r"PerimeterX", r"px-captcha", r"AkamaiGHost", r"Access Denied"],
    )
    if evidence.status_code in {401, 403, 429} or anti_bot_marker:
        marker = anti_bot_marker or f"HTTP {evidence.status_code}"
        return PlatformDetection(
            "browser_required", "Unknown", "HTTP/anti-bot response", f"marker={marker}", "high", url,
            "browser", "interactive challenge/session", "unknown", "unknown", "unknown", "true", "unknown", "true",
        )

    graphql_endpoint = _extract_endpoint(url, decoded, [r"https://[^\"'\s<>]+/graphql(?:\?[^\"'\s<>]*)?", r"/graphql(?:\?[^\"'\s<>]*)?"])
    if graphql_endpoint and _marker(decoded, [r"query\s+[A-Za-z_][A-Za-z0-9_]*", r"__typename", r"ApolloClient", r"graphql"]):
        return PlatformDetection(
            "custom_graphql", "First-party/custom", "page source endpoint", f"GraphQL endpoint={graphql_endpoint}", "medium",
            graphql_endpoint, "POST", "GraphQL query and variables", "cursor or query-specific", "unknown", "unknown", "true", "unknown", "false",
        )

    rest_endpoint = _extract_endpoint(
        url,
        decoded,
        [r"https://[^\"'\s<>]+/(?:api|services)/[^\"'\s<>]*(?:jobs|positions|vacancies|openings)[^\"'\s<>]*", r"/(?:api|services)/[^\"'\s<>]*(?:jobs|positions|vacancies|openings)[^\"'\s<>]*"],
    )
    if rest_endpoint:
        return PlatformDetection(
            "custom_rest_api", "First-party/custom", "page source endpoint", f"structured endpoint={rest_endpoint}", "medium",
            rest_endpoint, "GET/POST (verify)", "endpoint-specific filters", "endpoint-specific", "unknown", "unknown", "true", "unknown", "false",
        )

    embedded_marker = _marker(decoded, [r"__NEXT_DATA__", r"__INITIAL_STATE__", r"application/ld\+json", r"hydrationData", r"jobPosting"])
    job_id_marker = _marker(decoded, [r'"(?:jobId|job_id|requisitionId|requisition_id|postingId)"\s*:\s*"?[A-Za-z0-9_-]+'])
    apply_marker = _marker(decoded, [r'"(?:applyUrl|apply_url|applicationUrl)"\s*:\s*"?https?://', r'href=["\'][^"\']*(?:apply|application)[^"\']*["\']'])
    if embedded_marker and (_marker(decoded, [r'"@type"\s*:\s*"JobPosting"', r'"jobs"\s*:\s*\[', r'"positions"\s*:\s*\[']) or job_id_marker):
        return PlatformDetection(
            "javascript_embedded_data", "First-party/custom", "embedded page data",
            "; ".join(item for item in [f"marker={embedded_marker}", f"job id={job_id_marker}"] if item), "medium",
            url, "GET", "none for page; embedded-data schema varies", "page or embedded collection",
            "true" if job_id_marker else "unknown", "true" if apply_marker else "unknown", "true", "false", "false",
        )

    job_links = re.findall(r'href=["\']([^"\']*(?:/jobs?/|/positions?/|/vacancies/)[^"\']*)["\']', decoded, flags=re.IGNORECASE)
    script_count = len(re.findall(r"<script\b", decoded, flags=re.IGNORECASE))
    visible_text = re.sub(r"<script\b[^>]*>.*?</script>", "", decoded, flags=re.IGNORECASE | re.DOTALL)
    visible_text = re.sub(r"<[^>]+>", " ", visible_text)
    if 200 <= evidence.status_code < 300 and job_links and len(job_links) >= 2 and script_count < 20:
        stable = "true" if any(re.search(r"(?:jobs?|positions?)/[A-Za-z0-9_-]{3,}", link, flags=re.IGNORECASE) for link in job_links) else "unknown"
        return PlatformDetection(
            "static_html", "First-party/custom", "HTML job links", f"{len(job_links)} job-detail links in server HTML", "medium",
            url, "GET", "none or query parameters", "page/none", stable, "true", "false", "false", "false",
        )

    js_shell_marker = _marker(decoded, [r"<div[^>]+id=[\"'](?:root|app|__next)[\"']", r"enable JavaScript", r"noscript"])
    if 200 <= evidence.status_code < 300 and js_shell_marker and len(visible_text.strip()) < 1200:
        return PlatformDetection(
            "browser_required", "Unknown", "JavaScript application shell", f"marker={js_shell_marker}; visible text under 1200 chars", "medium",
            url, "browser", "JavaScript execution required", "unknown", "unknown", "unknown", "true", "unknown", "false",
        )

    return PlatformDetection(
        "unknown", "Unknown", "official page fetch", f"{evidence.note}; no recognized ATS/API/static-data signature", "low",
        url if evidence.status_code else "", "GET" if evidence.status_code else "", "unknown", "unknown",
        "unknown", "unknown", "unknown", "unknown", "false",
    )


def parent_source_detection(company: str, job_search_url: str) -> Optional[PlatformDetection]:
    rule = PARENT_SOURCE_RULES.get(normalize_company_name(company))
    if not rule:
        return None
    params = rule["parameters"]
    confidence = "high" if "safe structured filter not yet evidenced" not in params and "validate" not in params else "medium"
    return PlatformDetection(
        "parent_company_source", rule["parent"], "official shared-source URL",
        f"shared source={job_search_url}; parent={rule['parent']}", confidence, job_search_url, "GET", params,
        "parent adapter pagination", "true", "true", "false", "false", "false",
    )


def empty_fingerprint(source: Mapping[str, Any]) -> dict[str, str]:
    return {field: "" for field in FINGERPRINT_FIELDS} | {
        "Company": clean_text(source.get("Company")),
        "Official Careers URL": clean_text(source.get("Official Careers URL")),
        "Official Job Search URL": clean_text(source.get("Official Job Search URL")),
        "Current Research Status": clean_text(source.get("Research Status")),
    }


def build_fingerprint(source: Mapping[str, Any], evidence: FetchEvidence) -> dict[str, str]:
    company = clean_text(source.get("Company"))
    job_url = clean_text(source.get("Official Job Search URL"))
    detection = parent_source_detection(company, job_url) or detect_platform(evidence)
    parent_rule = PARENT_SOURCE_RULES.get(normalize_company_name(company), {})
    reviewed = detection.family != "unknown" and detection.confidence in {"high", "medium"}
    if detection.family == "browser_required":
        recommendation = "browser research required before adapter design"
    elif detection.family == "parent_company_source":
        recommendation = f"reuse {detection.vendor} source after validating filter and deduplication"
    elif detection.family in {"greenhouse", "lever", "ashby", "workday"}:
        recommendation = f"reuse existing {detection.family} adapter; do not register during this milestone"
    elif detection.family == "unknown":
        recommendation = "manual review; do not build an adapter from current evidence"
    elif detection.family in {"static_html", "javascript_embedded_data", "custom_graphql", "custom_rest_api"}:
        recommendation = f"consider reusable {detection.family} adapter only after endpoint contract validation"
    else:
        recommendation = f"candidate reusable {detection.family} adapter"
    notes = [
        detection.evidence_marker,
        f"Fetch: {evidence.note} at {evidence.final_url or evidence.requested_url}",
    ]
    if parent_rule:
        notes.append(parent_rule["notes"])
    if detection.family == "unknown":
        notes.append("Ambiguous evidence retained as unknown/manual review; no platform inferred.")
    return empty_fingerprint(source) | {
        "Detected Platform Family": detection.family,
        "Vendor": detection.vendor,
        "API or Endpoint URL": detection.endpoint_url,
        "Endpoint Method": detection.endpoint_method,
        "Required Parameters": detection.required_parameters,
        "Pagination Method": detection.pagination_method,
        "Stable Job ID Available": detection.stable_job_id,
        "Apply URL Available": detection.apply_url,
        "JavaScript Required": detection.javascript_required,
        "Authentication Required": detection.authentication_required,
        "Anti-Bot Observed": detection.anti_bot,
        "Parent Company Source": parent_rule.get("parent", ""),
        "Evidence URL": evidence.final_url or evidence.requested_url or job_url,
        "Evidence Type": detection.evidence_type,
        "Confidence": detection.confidence,
        "Reviewed": "true" if reviewed else "false",
        "Adapter Recommendation": recommendation,
        "Notes": " ".join(item for item in notes if item),
    }


def read_csv(path: Path, required_fields: Iterable[str]) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        missing = [field for field in required_fields if field not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path} is missing columns: {', '.join(missing)}")
        return [{key: clean_text(value) for key, value in row.items()} for row in reader]


def merge_fingerprint_batch(
    source_rows: list[Mapping[str, Any]],
    existing_rows: list[Mapping[str, Any]],
    client: ControlledFingerprintClient,
    *,
    start_row: int = 1,
    limit: Optional[int] = None,
    batch_size: int = 25,
    resume: bool = False,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if start_row < 1:
        raise ValueError("start_row must be at least 1")
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    by_name = {normalize_company_name(row.get("Company")): dict(row) for row in existing_rows}
    selected = source_rows[start_row - 1 :]
    if limit is not None:
        selected = selected[:limit]
    decisions: list[dict[str, str]] = []
    for offset in range(0, len(selected), batch_size):
        batch = selected[offset : offset + batch_size]
        if len(batch) > MAX_BATCH_SIZE:  # Defensive invariant for future refactors.
            raise AssertionError("internal batch exceeded 25 companies")
        for source in batch:
            company = clean_text(source.get("Company"))
            key = normalize_company_name(company)
            previous = by_name.get(key)
            if previous and parse_bool(previous.get("Reviewed")):
                decisions.append({"company": company, "decision": "preserved_reviewed"})
                continue
            if resume and previous and clean_text(previous.get("Detected Platform Family")):
                decisions.append({"company": company, "decision": "resume_skipped_completed"})
                continue
            job_url = clean_text(source.get("Official Job Search URL")) or clean_text(source.get("Official Careers URL"))
            try:
                evidence = client.fetch(job_url)
            except requests.RequestException as exc:
                evidence = FetchEvidence(job_url, job_url, 0, "", "", f"Request failed: {exc.__class__.__name__}: {exc}", {})
            result = build_fingerprint(source, evidence)
            by_name[key] = result
            decisions.append({"company": company, "decision": result["Detected Platform Family"]})
    ordered = [
        by_name[normalize_company_name(source.get("Company"))]
        for source in source_rows
        if normalize_company_name(source.get("Company")) in by_name
    ]
    return ordered, decisions


def _yes_share(rows: list[Mapping[str, Any]], field: str) -> float:
    known = [clean_text(row.get(field)).casefold() for row in rows if clean_text(row.get(field)).casefold() in {"true", "false"}]
    return sum(value == "true" for value in known) / len(known) if known else 0.0


def adapter_priority_score(rows: list[Mapping[str, Any]]) -> tuple[int, dict[str, str]]:
    count = len(rows)
    confidence_points = {"high": 20, "medium": 12, "low": 4, "": 0}
    average_confidence = round(sum(confidence_points.get(clean_text(row.get("Confidence")), 0) for row in rows) / count) if count else 0
    structured_share = sum(_structured_endpoint(row) for row in rows) / count if count else 0
    browser_share = sum(
        clean_text(row.get("Detected Platform Family")) == "browser_required" or clean_text(row.get("JavaScript Required")) == "true" and clean_text(row.get("API or Endpoint URL")) == clean_text(row.get("Official Job Search URL"))
        for row in rows
    ) / count if count else 0
    anti_bot_share = _yes_share(rows, "Anti-Bot Observed")
    stable_share = _yes_share(rows, "Stable Job ID Available")
    apply_share = _yes_share(rows, "Apply URL Available")
    pagination_share = sum(
        clean_text(row.get("Pagination Method")) not in {"", "unknown", "page or embedded collection", "endpoint-specific", "parent adapter pagination"}
        for row in rows
    ) / count if count else 0
    family = clean_text(rows[0].get("Detected Platform Family")) if rows else ""
    family_complexity = {
        "greenhouse": "low", "lever": "low", "ashby": "low", "workday": "low",
        "smartrecruiters": "low", "custom_graphql": "medium", "custom_rest_api": "medium",
        "static_html": "medium", "javascript_embedded_data": "high", "icims": "medium",
        "sap_successfactors": "medium", "phenom": "medium", "eightfold": "medium",
        "oracle_recruiting": "medium", "oracle_taleo": "high", "avature": "high",
    }.get(family)
    complexity = family_complexity or ("low" if structured_share >= 0.8 and browser_share == 0 else "high" if browser_share >= 0.5 or structured_share < 0.25 else "medium")
    complexity_points = {"low": 15, "medium": 8, "high": 2}[complexity]
    score = round(
        min(count * 8, 35)
        + average_confidence
        + structured_share * 10
        + pagination_share * 5
        + complexity_points
        + (1 - anti_bot_share) * 5
        + stable_share * 5
        + apply_share * 5
    )
    score = max(0, min(100, score))
    return score, {
        "Implementation Complexity": complexity,
        "Stable Public Endpoint Share": f"{structured_share:.2f}",
        "Anti-Bot Risk Share": f"{anti_bot_share:.2f}",
        "Stable Job ID Share": f"{stable_share:.2f}",
        "Apply URL Share": f"{apply_share:.2f}",
        "Deterministic Pagination Share": f"{pagination_share:.2f}",
    }


def _structured_endpoint(row: Mapping[str, Any]) -> bool:
    endpoint = clean_text(row.get("API or Endpoint URL")).casefold()
    method = clean_text(row.get("Endpoint Method")).casefold()
    if not endpoint or method in {"", "browser"}:
        return False
    return any(marker in endpoint for marker in (
        "/wday/cxs/", "/hcmrestapi/", "/graphql", "/api/", "api.",
        "boards-api.greenhouse.io", "api.lever.co",
    ))


def generate_reports(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    families: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        families[clean_text(row.get("Detected Platform Family")) or "unknown"].append(row)
    summary_rows = []
    for family, members in families.items():
        confidences = Counter(clean_text(row.get("Confidence")) or "low" for row in members)
        summary_rows.append({
            "Platform Family": family,
            "Companies": len(members),
            "High Confidence": confidences["high"],
            "Medium Confidence": confidences["medium"],
            "Low Confidence": confidences["low"],
            "Reviewed": sum(parse_bool(row.get("Reviewed")) for row in members),
            "Stable Structured Endpoint": sum(_structured_endpoint(row) for row in members),
            "Browser Required": sum(clean_text(row.get("Detected Platform Family")) == "browser_required" for row in members),
        })
    summary_rows.sort(key=lambda row: (-int(row["Companies"]), str(row["Platform Family"])))
    write_csv(PLATFORM_SUMMARY, summary_rows, [
        "Platform Family", "Companies", "High Confidence", "Medium Confidence", "Low Confidence",
        "Reviewed", "Stable Structured Endpoint", "Browser Required",
    ])
    write_csv(FINGERPRINT_DETAILS, rows, FINGERPRINT_FIELDS)
    manual_rows = [row for row in rows if not parse_bool(row.get("Reviewed")) or clean_text(row.get("Detected Platform Family")) in {"unknown", "browser_required"}]
    write_csv(MANUAL_REVIEW, manual_rows, FINGERPRINT_FIELDS)

    excluded = {"unknown", "browser_required", "parent_company_source"}
    priority_rows = []
    for family, members in families.items():
        if family in excluded:
            continue
        score, metrics = adapter_priority_score(members)
        companies = sorted(clean_text(row.get("Company")) for row in members)
        priority_rows.append({
            "Rank": "",
            "Platform Family": family,
            "Priority Score": score,
            "Companies Potentially Unlocked": len(members),
            "High Confidence": sum(clean_text(row.get("Confidence")) == "high" for row in members),
            **metrics,
            "Companies": "; ".join(companies),
            "Recommendation": f"Build {family} adapter" if family not in {"greenhouse", "lever", "ashby", "workday"} else f"Validate and reuse existing {family} adapter",
        })
    priority_rows.sort(key=lambda row: (-int(row["Priority Score"]), -int(row["Companies Potentially Unlocked"]), str(row["Platform Family"])))
    for index, row in enumerate(priority_rows, 1):
        row["Rank"] = index
    priority_fields = [
        "Rank", "Platform Family", "Priority Score", "Companies Potentially Unlocked", "High Confidence",
        "Stable Public Endpoint Share", "Implementation Complexity", "Anti-Bot Risk Share", "Stable Job ID Share",
        "Apply URL Share", "Deterministic Pagination Share", "Companies", "Recommendation",
    ]
    write_csv(ADAPTER_PRIORITY, priority_rows, priority_fields)

    parent_rows = []
    for row in rows:
        if clean_text(row.get("Detected Platform Family")) != "parent_company_source":
            continue
        parent_rows.append({
            "Company": clean_text(row.get("Company")),
            "Parent Company": clean_text(row.get("Parent Company Source")),
            "Shared Endpoint": clean_text(row.get("API or Endpoint URL")),
            "Safe Filter / Required Parameters": clean_text(row.get("Required Parameters")),
            "Duplicate-Source Risk": "high; shared parent source, deduplicate by parent source and stable job ID",
            "Confidence": clean_text(row.get("Confidence")),
            "Evidence URL": clean_text(row.get("Evidence URL")),
            "Notes": clean_text(row.get("Notes")),
        })
    write_csv(PARENT_SOURCES, parent_rows, [
        "Company", "Parent Company", "Shared Endpoint", "Safe Filter / Required Parameters",
        "Duplicate-Source Risk", "Confidence", "Evidence URL", "Notes",
    ])
    confidences = Counter(clean_text(row.get("Confidence")) or "low" for row in rows)
    return {
        "companies_fingerprinted": len(rows),
        "family_counts": {row["Platform Family"]: row["Companies"] for row in summary_rows},
        "high_confidence": confidences["high"],
        "medium_confidence": confidences["medium"],
        "low_confidence": confidences["low"],
        "unknown_or_manual_review": len(manual_rows),
        "parent_company_sources": len(parent_rows),
        "adapter_priorities": priority_rows,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--research-file", type=Path, default=DEFAULT_RESEARCH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start-row", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=10)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.batch_size > MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    # Read the workbook to validate the requested source and retain its explicit role
    # in the CLI contract. Research rows carry the reviewed official URLs.
    workbook_names = {normalize_company_name(row.get("Company")) for row in read_workbook(args.input)}
    research_rows = read_csv(args.research_file, ["Company", "Official Careers URL", "Official Job Search URL", "Research Status"])
    source_rows = [
        row for row in research_rows
        if clean_text(row.get("Research Status")) not in READY_RESEARCH_STATUSES
        and normalize_company_name(row.get("Company")) in workbook_names
    ]
    existing_rows = read_csv(args.output, FINGERPRINT_FIELDS) if args.output.exists() else []
    if args.dry_run:
        selected = source_rows[args.start_row - 1 :]
        if args.limit is not None:
            selected = selected[: args.limit]
        print(json.dumps({
            "dry_run": True,
            "companies_in_scope": len(source_rows),
            "selected": [clean_text(row.get("Company")) for row in selected],
            "existing_fingerprints": len(existing_rows),
            "batch_size": args.batch_size,
        }, indent=2, ensure_ascii=False))
        return 0

    client = ControlledFingerprintClient(delay=args.delay, retries=args.retries, timeout=args.timeout)
    selected_count = len(source_rows[args.start_row - 1 :])
    if args.limit is not None:
        selected_count = min(selected_count, args.limit)
    rows = existing_rows
    decisions: list[dict[str, str]] = []
    # Persist after every bounded batch. A terminated run can therefore continue
    # with --resume without repeating completed requests or losing reviewed rows.
    for batch_offset in range(0, selected_count, args.batch_size):
        batch_limit = min(args.batch_size, selected_count - batch_offset)
        rows, batch_decisions = merge_fingerprint_batch(
            source_rows,
            rows,
            client,
            start_row=args.start_row + batch_offset,
            limit=batch_limit,
            batch_size=args.batch_size,
            resume=args.resume,
        )
        decisions.extend(batch_decisions)
        write_csv(args.output, rows, FINGERPRINT_FIELDS)
        generate_reports(rows)
        print(
            f"Persisted batch {batch_offset // args.batch_size + 1}: "
            f"{batch_limit} selected; {len(rows)} total fingerprints",
            flush=True,
        )
    summary = generate_reports(rows)
    print(json.dumps({"decisions": decisions, "summary": summary}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
