"""Research one resumable watchlist batch using official-domain evidence only."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from dotenv import load_dotenv

try:
    from scripts.company_watchlist_common import (
        ATSDetection,
        DETECTED_BUT_UNSUPPORTED,
        PROHIBITED_JOB_BOARD_DOMAINS,
        RESEARCH_FIELDS,
        SUPPORTED_IMPORT_PLATFORMS,
        _safe_public_https_target,
        canonicalize_url,
        clean_text,
        detect_ats,
        detection_from_registry,
        load_registry,
        normalize_company_name,
        parse_bool,
        prohibited_job_board,
        read_research_csv,
        registry_by_name,
        registry_source_url,
        validate_detection_fields,
        write_csv,
    )
    from scripts.enrich_company_watchlist import read_workbook
except ModuleNotFoundError:
    from company_watchlist_common import (  # type: ignore[no-redef]
        ATSDetection,
        DETECTED_BUT_UNSUPPORTED,
        PROHIBITED_JOB_BOARD_DOMAINS,
        RESEARCH_FIELDS,
        SUPPORTED_IMPORT_PLATFORMS,
        _safe_public_https_target,
        canonicalize_url,
        clean_text,
        detect_ats,
        detection_from_registry,
        load_registry,
        normalize_company_name,
        parse_bool,
        prohibited_job_board,
        read_research_csv,
        registry_by_name,
        registry_source_url,
        validate_detection_fields,
        write_csv,
    )
    from enrich_company_watchlist import read_workbook  # type: ignore[no-redef]


DEFAULT_INPUT = Path("config/company_watchlist.xlsx")
DEFAULT_RESEARCH = Path("config/company_watchlist_research.csv")
PLATFORM_SUMMARY = Path("output/company_watchlist_platform_summary.csv")
RESEARCH_SUMMARY = Path("output/company_watchlist_research_summary.json")
MANUAL_REVIEW_REPORT = Path("output/company_watchlist_manual_review.csv")
CUSTOM_ADAPTER_REPORT = Path("output/company_watchlist_custom_adapters.csv")
TAVILY_URL = "https://api.tavily.com/search"
DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"
MAX_BATCH_SIZE = 25
SUPPORTED_GENERIC = {"greenhouse", "lever", "ashby", "workday"}
CAREER_TERMS = ("career", "jobs", "job-search", "opportunities", "join-us", "open-roles")
SEARCH_EXCLUDES = sorted(PROHIBITED_JOB_BOARD_DOMAINS | {"jooble.org", "careerjet.com"})


class ControlledResearchClient:
    """Sequential HTTP client with explicit pacing and bounded retries."""

    def __init__(
        self,
        api_key: str,
        *,
        delay: float = 1.0,
        retries: int = 2,
        timeout: int = 20,
        session: Optional[requests.Session] = None,
        sleep_fn: Any = time.sleep,
    ) -> None:
        if delay < 0:
            raise ValueError("delay cannot be negative")
        if retries < 0 or retries > 5:
            raise ValueError("retries must be between 0 and 5")
        self.api_key = api_key
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.session = session or requests.Session()
        self.sleep_fn = sleep_fn
        self.tavily_available = bool(api_key)

    def search(self, company: str) -> list[dict[str, Any]]:
        query = f'"{company}" official careers jobs'
        if self.tavily_available:
            try:
                response = self._request(
                    "post",
                    TAVILY_URL,
                    json={
                        "api_key": self.api_key,
                        "query": query,
                        "search_depth": "basic",
                        "max_results": 7,
                        "exclude_domains": SEARCH_EXCLUDES,
                        "include_answer": False,
                        "include_raw_content": False,
                        "include_images": False,
                    },
                )
                return [
                    item for item in response.json().get("results", [])
                    if isinstance(item, dict)
                ]
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code not in {401, 403}:
                    raise
                self.tavily_available = False
        response = self._request(
            "get",
            DUCKDUCKGO_URL,
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        parser = _SearchResultParser()
        parser.feed(response.text)
        return parser.results[:10]

    def fetch(self, url: str) -> tuple[bool, str, str, str]:
        current = canonicalize_url(url)
        for _ in range(6):
            safe, note = _safe_public_https_target(current)
            if not safe:
                return False, current, "", note
            response = self._request(
                "get",
                current,
                allow_redirects=False,
                stream=True,
                headers={
                    "User-Agent": "JobRadarWatchlistResearch/1.0",
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                },
            )
            try:
                status = int(response.status_code)
                if status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location", "")
                    if not location:
                        return False, current, "", f"HTTP {status} redirect without Location"
                    current = canonicalize_url(urljoin(current, location))
                    continue
                if not 200 <= status < 300:
                    return False, current, "", f"HTTP {status}"
                content_type = response.headers.get("Content-Type", "")
                if content_type and "html" not in content_type.casefold():
                    return True, current, "", f"HTTP {status}; non-HTML response"
                raw = response.raw.read(2_000_001, decode_content=True)
                if len(raw) > 2_000_000:
                    return False, current, "", "HTML response exceeded 2 MB"
                return True, current, raw.decode(response.encoding or "utf-8", errors="replace"), f"HTTP {status}"
            finally:
                response.close()
        return False, current, "", "Too many redirects"

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            self.sleep_fn(self.delay)
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    response.close()
                    raise requests.RequestException(f"HTTP {response.status_code}")
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                if isinstance(exc, requests.HTTPError) and exc.response is not None:
                    if 400 <= exc.response.status_code < 500 and exc.response.status_code != 429:
                        raise
                if attempt < self.retries:
                    self.sleep_fn(self.delay * (attempt + 1))
        raise requests.RequestException(
            f"Request failed after {self.retries + 1} attempts: {last_error}"
        )


def existing_research_row(company: str, entry: Mapping[str, Any]) -> dict[str, str]:
    detection = detection_from_registry(entry)
    source_url = registry_source_url(entry)
    return research_row(
        company=company,
        careers_url=source_url,
        job_search_url=source_url,
        platform=detection.platform,
        required_adapter=detection.platform,
        reusable=True,
        source_url=source_url,
        status="existing_config",
        notes="Preserved existing validated registry configuration.",
        reviewed=True,
        detection=detection,
    )


def research_company(company: str, client: ControlledResearchClient) -> dict[str, str]:
    try:
        results = client.search(company)
    except requests.RequestException as exc:
        return empty_manual(company, f"Search request failed: {exc}")
    candidates = official_candidates(company, results)
    if not candidates:
        return empty_manual(company, "No official-domain or confirmed ATS candidate found")

    careers_url = candidates[0]["url"]
    evidence_url = careers_url
    fetch_ok = False
    final_url = careers_url
    html = ""
    fetch_note = "not fetched"
    try:
        fetch_ok, final_url, html, fetch_note = client.fetch(careers_url)
    except requests.RequestException as exc:
        fetch_note = f"Fetch request failed: {exc}"
    links = career_links(final_url, html) if html else []
    job_search_url, detection = select_job_search(final_url, detect_ats(careers_url), links)
    fields_valid, fields_note = validate_detection_fields(detection)

    if detection.platform in SUPPORTED_GENERIC and fields_valid and fetch_ok:
        return research_row(
            company, careers_url, job_search_url, detection.platform, detection.platform,
            True, evidence_url, "validated_supported",
            f"Official careers page exposed complete {detection.platform} identifiers; {fetch_note}.",
            True, detection,
        )
    if detection.platform in DETECTED_BUT_UNSUPPORTED and detection.slug and fetch_ok:
        return research_row(
            company, careers_url, job_search_url, detection.platform, detection.platform,
            False, evidence_url, "validated_custom_adapter_required",
            f"Official careers page exposed {detection.platform}; no scanner adapter exists; {fetch_note}.",
            True, detection,
        )
    if fetch_ok and is_clear_job_search(job_search_url):
        return research_row(
            company, careers_url, job_search_url, "custom", "custom_company_site",
            False, evidence_url, "validated_custom_adapter_required",
            f"Official job search confirmed, but no supported ATS identifiers were exposed; {fetch_note}.",
            True, ATSDetection(platform="custom"),
        )
    return research_row(
        company, careers_url, job_search_url, detection.platform or "unknown", "unknown",
        False, evidence_url, "manual_review",
        f"Platform evidence remains incomplete ({fields_note}; {fetch_note}).",
        fetch_ok, detection,
    )


def official_candidates(company: str, results: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    scored: list[tuple[int, dict[str, str]]] = []
    tokens = company_tokens(company)
    for item in results:
        url = canonicalize_url(item.get("url"))
        if not url or prohibited_job_board(url):
            continue
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        title = clean_text(item.get("title"))
        content = clean_text(item.get("content"))
        detection = detect_ats(url)
        company_evidence = normalize_company_name(company) in normalize_company_name(title + " " + content)
        company_domain = host_matches_tokens(host, tokens)
        if not company_domain and not (detection.platform and company_evidence):
            continue
        searchable = f"{host} {parsed.path} {title}".casefold()
        career_score = sum(3 for term in CAREER_TERMS if term in searchable)
        if career_score == 0 and not detection.platform:
            continue
        # A provider hit is useful evidence only when its page names the company.
        # Prefer the company's own domain decisively so similarly named companies
        # and broad provider pages cannot outrank an official careers page.
        score = career_score + (20 if company_domain else 0) + (3 if detection.platform else 0)
        scored.append((score, {"url": url, "title": title, "content": content}))
    return [item for _, item in sorted(scored, key=lambda pair: (-pair[0], pair[1]["url"]))]


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.casefold() != "a":
            return
        self._href = next((value or "" for key, value in attrs if key.casefold() == "href"), "")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href:
            self.links.append((self._href, " ".join(self._text)))
            self._href = ""
            self._text = []


class _SearchResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._href = ""
        self._is_result = False
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.casefold() != "a":
            return
        values = dict(attrs)
        self._is_result = "result__a" in (values.get("class") or "")
        self._href = values.get("href") or ""
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._is_result:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "a" or not self._is_result:
            return
        parsed = urlsplit(self._href)
        destination = parse_qs(parsed.query).get("uddg", [self._href])[0]
        self.results.append({
            "url": destination,
            "title": " ".join(self._text).strip(),
            "content": "",
        })
        self._href = ""
        self._is_result = False
        self._text = []


def career_links(base_url: str, html: str) -> list[str]:
    links: set[str] = set()
    parser = _LinkParser()
    parser.feed(html)
    for href, anchor_text in parser.links:
        url = canonicalize_url(urljoin(base_url, href))
        if not url or prohibited_job_board(url) or urlsplit(url).scheme != "https":
            continue
        parsed = urlsplit(url)
        searchable = f"{parsed.hostname} {parsed.path} {anchor_text}".casefold()
        if detect_ats(url).platform or any(term in searchable for term in CAREER_TERMS):
            links.add(url)
    return sorted(links)


def select_job_search(
    final_url: str,
    direct_detection: ATSDetection,
    links: list[str],
) -> tuple[str, ATSDetection]:
    final_detection = detect_ats(final_url)
    for url in [final_url, *links]:
        detection = detect_ats(url)
        fields_valid, _ = validate_detection_fields(detection)
        if fields_valid or detection.platform in DETECTED_BUT_UNSUPPORTED:
            return url, detection
    same_host = [url for url in links if urlsplit(url).hostname == urlsplit(final_url).hostname]
    clear_searches = [url for url in same_host if is_clear_job_search(url)]
    if clear_searches:
        return clear_searches[0], ATSDetection(platform="custom")
    return final_url, final_detection if final_detection.platform else direct_detection


def is_clear_job_search(url: str) -> bool:
    path = urlsplit(url).path.casefold()
    return any(term in path for term in ("/jobs", "/job-search", "/search", "/open-roles", "/positions"))


def company_tokens(company: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", company.casefold())
    ignored = {"the", "group", "holdings", "international", "technologies", "technology", "systems"}
    return [token for token in tokens if token not in ignored and (len(token) >= 3 or len(tokens) == 1)]


def host_matches_tokens(host: str, tokens: list[str]) -> bool:
    host_key = normalize_company_name(host)
    labels = host.split(".")
    return any(token in host_key or token in labels for token in tokens)


def research_row(
    company: str,
    careers_url: str,
    job_search_url: str,
    platform: str,
    required_adapter: str,
    reusable: bool,
    source_url: str,
    status: str,
    notes: str,
    reviewed: bool,
    detection: ATSDetection,
) -> dict[str, str]:
    return {
        "Company": company,
        "Official Careers URL": careers_url,
        "Official Job Search URL": job_search_url,
        "Detected Platform": platform,
        "Required Adapter": required_adapter,
        "Existing Adapter Reusable": "true" if reusable else "false",
        "Source URL": source_url,
        "Research Status": status,
        "Research Notes": notes,
        "Reviewed": "true" if reviewed else "false",
        "ATS Slug": detection.slug,
        "Workday Tenant": detection.tenant,
        "Workday Subdomain": detection.wd_subdomain,
        "Workday Site ID": detection.site_id,
    }


def empty_manual(company: str, notes: str) -> dict[str, str]:
    return research_row(
        company, "", "", "unknown", "unknown", False, "", "manual_review", notes,
        False, ATSDetection(),
    )


def merge_batch(
    workbook_rows: list[Mapping[str, Any]],
    research_rows: list[Mapping[str, Any]],
    registry_entries: list[Mapping[str, Any]],
    client: ControlledResearchClient,
    *,
    start_row: int,
    batch_size: int,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if start_row < 1:
        raise ValueError("start_row must be at least 1")
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    by_name = {normalize_company_name(row.get("Company")): dict(row) for row in research_rows}
    registry = registry_by_name(registry_entries)
    selected = workbook_rows[start_row - 1 : start_row - 1 + batch_size]
    decisions: list[dict[str, str]] = []
    for source in selected:
        company = clean_text(source.get("Company"))
        key = normalize_company_name(company)
        previous = by_name.get(key)
        if previous and parse_bool(previous.get("Reviewed")):
            decisions.append({"company": company, "decision": "preserved_reviewed"})
            continue
        if key in registry:
            result = existing_research_row(company, registry[key])
        else:
            result = research_company(company, client)
        by_name[key] = result
        decisions.append({"company": company, "decision": result["Research Status"]})
    ordered = [
        by_name[normalize_company_name(row.get("Company"))]
        for row in workbook_rows
        if normalize_company_name(row.get("Company")) in by_name
    ]
    return ordered, decisions


def generate_reports(
    workbook_rows: list[Mapping[str, Any]],
    research_rows: list[Mapping[str, Any]],
    registry_entries: list[Mapping[str, Any]],
) -> dict[str, Any]:
    by_name = {normalize_company_name(row.get("Company")): row for row in research_rows}
    registry_names = set(registry_by_name(registry_entries))
    workbook_names = {normalize_company_name(row.get("Company")) for row in workbook_rows}
    platform_counts: dict[str, Counter[str]] = defaultdict(Counter)
    manual_rows: list[Mapping[str, Any]] = []
    custom_rows: list[dict[str, str]] = []
    duplicates = Counter(normalize_company_name(row.get("Company")) for row in workbook_rows)
    for source in workbook_rows:
        key = normalize_company_name(source.get("Company"))
        row = by_name.get(key, empty_manual(clean_text(source.get("Company")), "Not researched"))
        platform = clean_text(row.get("Detected Platform")) or "unknown"
        status = clean_text(row.get("Research Status")) or "manual_review"
        bucket = platform_counts[platform]
        bucket["total"] += 1
        bucket[{"existing_config": "existing", "validated_supported": "new_supported", "validated_custom_adapter_required": "custom", "invalid": "invalid"}.get(status, "manual")] += 1
        if status in {"manual_review", "invalid"} or not parse_bool(row.get("Reviewed")):
            manual_rows.append(row)
        required_adapter = clean_text(row.get("Required Adapter"))
        custom_candidate = (
            status == "validated_custom_adapter_required"
            or (
                status == "manual_review"
                and not parse_bool(row.get("Existing Adapter Reusable"))
                and required_adapter not in {"", "unknown"}
                and platform not in SUPPORTED_IMPORT_PLATFORMS
            )
        )
        if custom_candidate:
            custom_rows.append({
                "Adapter Family": required_adapter or platform,
                "Company": clean_text(row.get("Company")),
                "Platform": platform,
                "Research Status": status,
                "Official Job Search URL": clean_text(row.get("Official Job Search URL")),
                "Reviewed": clean_text(row.get("Reviewed")),
            })

    platform_fields = [
        "Platform", "Total companies", "Existing supported", "Newly validated supported",
        "Custom adapter required", "Manual review", "Invalid", "Percentage of full watchlist",
    ]
    platform_rows = []
    total = len(workbook_rows)
    for platform in sorted(platform_counts):
        counts = platform_counts[platform]
        platform_rows.append({
            "Platform": platform,
            "Total companies": counts["total"],
            "Existing supported": counts["existing"],
            "Newly validated supported": counts["new_supported"],
            "Custom adapter required": counts["custom"],
            "Manual review": counts["manual"],
            "Invalid": counts["invalid"],
            "Percentage of full watchlist": f"{counts['total'] / total * 100:.2f}" if total else "0.00",
        })
    write_csv(PLATFORM_SUMMARY, platform_rows, platform_fields)
    write_csv(MANUAL_REVIEW_REPORT, manual_rows, RESEARCH_FIELDS)
    adapter_counts = Counter(row["Adapter Family"] for row in custom_rows)
    for row in custom_rows:
        row["Companies Unlocked"] = str(adapter_counts[row["Adapter Family"]])
    write_csv(
        CUSTOM_ADAPTER_REPORT,
        sorted(
            custom_rows,
            key=lambda row: (-adapter_counts[row["Adapter Family"]], row["Adapter Family"], row["Company"]),
        ),
        [
            "Adapter Family", "Companies Unlocked", "Company", "Platform",
            "Research Status", "Official Job Search URL", "Reviewed",
        ],
    )

    statuses = Counter(clean_text(row.get("Research Status")) for row in by_name.values())
    ready = statuses["existing_config"] + statuses["validated_supported"]
    unreviewed_supported = sum(
        1 for row in by_name.values()
        if not parse_bool(row.get("Reviewed"))
        and clean_text(row.get("Detected Platform")) in SUPPORTED_IMPORT_PLATFORMS
    )
    summary = {
        "total_workbook_companies": total,
        "researched_companies": len(by_name),
        "previously_configured_companies": len(registry_names & workbook_names),
        "supported_and_ready_for_import": ready,
        "supported_but_requiring_validation": unreviewed_supported,
        "unsupported_adapter_families": sorted({row["Adapter Family"] for row in custom_rows}),
        "manual_review_count": len(manual_rows),
        "missing_official_careers_url_count": sum(
            1 for row in by_name.values() if not clean_text(row.get("Official Careers URL"))
        ),
        "invalid_count": statuses["invalid"],
        "duplicate_count": sum(count - 1 for count in duplicates.values() if count > 1),
        "estimated_scanner_coverage_percentage": round(ready / total * 100, 2) if total else 0.0,
        "status_counts": dict(sorted(statuses.items())),
    }
    RESEARCH_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    RESEARCH_SUMMARY.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--research-file", type=Path, default=DEFAULT_RESEARCH)
    parser.add_argument("--start-row", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reports-only", action="store_true")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    workbook_rows = read_workbook(args.input)
    _, registry_entries = load_registry()
    research_rows = read_research_csv(args.research_file) if args.research_file.exists() else []
    decisions: list[dict[str, str]] = []
    if not args.reports_only:
        load_dotenv()
        api_key = os.getenv("TAVILY_API_KEY", "")
        if not api_key:
            raise RuntimeError("TAVILY_API_KEY is required for live research")
        client = ControlledResearchClient(api_key, delay=args.delay, retries=args.retries)
        research_rows, decisions = merge_batch(
            workbook_rows,
            research_rows,
            registry_entries,
            client,
            start_row=args.start_row,
            batch_size=args.batch_size,
        )
        if not args.dry_run:
            write_csv(args.research_file, research_rows, RESEARCH_FIELDS)
    summary = generate_reports(workbook_rows, research_rows, registry_entries) if not args.dry_run else {}
    print(json.dumps({
        "start_row": args.start_row,
        "batch_size": args.batch_size,
        "dry_run": args.dry_run,
        "decisions": decisions,
        "summary": summary,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
