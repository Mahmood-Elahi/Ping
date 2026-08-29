"""SAP SuccessFactors Recruiting Marketing (RMK/Jobs2Web) adapter.

The supported contracts are the two public HTML layouts observed on the
configured company-owned career domains: a paginated table and a tile list
whose subsequent pages are returned as HTML fragments.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any, Iterable, List, Mapping, Optional
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlparse, urlunparse

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


PAGE_SIZE = 25
MAX_TILE_PAGE_SIZE = 100
MAX_PAGES = 5
MAX_RESULTS = PAGE_SIZE * MAX_PAGES
MAX_TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 0.5
PAGE_DELAY_SECONDS = 0.2
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
TABLE_VARIANT = "rmk_table"
TILE_VARIANT = "rmk_tile"
COLUMNIZED_VARIANT = "rmk_columnized"
SUPPORTED_VARIANTS = {TABLE_VARIANT, TILE_VARIANT, COLUMNIZED_VARIANT}
SEARCH_PATH_SUFFIX = "/search/"
TILE_ENDPOINT = "tile-search-results"
COLUMNIZED_ENDPOINT = "/services/recruiting/v1/jobs"
COLUMNIZED_PAGE_SIZE = 10

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/xhtml+xml",
}


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: Optional["_Node"] = None
    children: list["_Node"] = field(default_factory=list)
    content: list[Any] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return {value.casefold() for value in self.attrs.get("class", "").split()}


class _DomParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        node = _Node(
            tag.casefold(),
            {str(key).casefold(): str(value or "") for key, value in attrs},
            self.stack[-1],
        )
        self.stack[-1].children.append(node)
        self.stack[-1].content.append(node)
        if tag.casefold() not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        wanted = tag.casefold()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == wanted:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].content.append(data)


def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> List[JobRecord]:
    """Fetch a bounded, deterministically ordered public RMK job collection."""
    validate_target(target)
    timeout = max(1, min(int(timeout), MAX_TIMEOUT_SECONDS))
    if target.endpoint_variant == COLUMNIZED_VARIANT:
        return _fetch_columnized_jobs(session, target, timeout)
    max_pages = max(1, int(target.max_pages))
    if max_pages > MAX_PAGES:
        raise AdapterError(f"SAP SuccessFactors max_pages cannot exceed {MAX_PAGES}")

    metadata: dict[str, Any] = {
        "endpoint_variant": target.endpoint_variant,
        "pages_fetched": 0,
        "page_size": PAGE_SIZE,
        "maximum_pages": max_pages,
        "maximum_results": min(MAX_RESULTS, max_pages * PAGE_SIZE),
        "total_available": None,
        "jobs_normalized": 0,
        "offsets": [],
        "pages": [],
        "stop_reason": "not_started",
        "diagnostics": [],
    }
    setattr(session, "sap_successfactors_pagination", metadata)

    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    current_offset = 0
    next_request: Optional[tuple[str, Optional[dict[str, str]]]] = (
        str(target.search_url),
        None,
    )

    for page_index in range(max_pages):
        if next_request is None or len(jobs) >= metadata["maximum_results"]:
            break
        offset = current_offset
        url, params = next_request
        response = _get_page(session, target, url, params, offset, timeout)
        page = _parse_page(
            response,
            target,
            offset,
            expected_page_size=(metadata["page_size"] if page_index else None),
        )

        if metadata["total_available"] is None:
            metadata["total_available"] = page.total
        elif page.total is not None and page.total != metadata["total_available"]:
            raise AdapterError("SAP SuccessFactors total job count changed during pagination")
        if page_index == 0:
            metadata["page_size"] = page.page_size
            metadata["maximum_results"] = min(
                MAX_RESULTS,
                max_pages * page.page_size,
            )
        elif page.page_size != metadata["page_size"]:
            raise AdapterError("SAP SuccessFactors page size changed during pagination")

        normalized_count = 0
        for item in page.items:
            try:
                job = _normalize_item(item, target)
            except AdapterError as exc:
                metadata["diagnostics"].append(
                    {"offset": offset, "error": str(exc), "item": dict(item)}
                )
                continue
            if job.job_id in seen_ids:
                metadata["diagnostics"].append(
                    {"offset": offset, "error": f"Duplicate stable job ID: {job.job_id}"}
                )
                continue
            seen_ids.add(job.job_id)
            jobs.append(job)
            normalized_count += 1
            if len(jobs) >= metadata["maximum_results"]:
                break

        if page.items and normalized_count == 0:
            raise AdapterError("SAP SuccessFactors page contained jobs but none were valid")
        if page.total and offset < page.total and not page.items:
            raise AdapterError("SAP SuccessFactors response ended before its reported total job count")

        metadata["pages"].append(
            {
                "offset": offset,
                "url": getattr(response, "url", url),
                "jobs_returned": len(page.items),
                "jobs_normalized": normalized_count,
            }
        )
        metadata["pages_fetched"] = len(metadata["pages"])
        metadata["offsets"].append(offset)
        metadata["jobs_normalized"] = len(jobs)

        effective_total = metadata["total_available"]
        if effective_total == 0:
            metadata["stop_reason"] = "validated_zero_jobs"
            next_request = None
        elif effective_total is not None and offset + len(page.items) >= effective_total:
            metadata["stop_reason"] = "total_jobs_reached"
            next_request = None
        elif len(page.items) < page.page_size:
            metadata["stop_reason"] = "short_page"
            next_request = None
        else:
            next_request = page.next_request
            if (
                next_request is None
                and target.endpoint_variant == TILE_VARIANT
                and params is not None
            ):
                next_params = dict(params)
                next_params["startrow"] = str(offset + page.page_size)
                next_request = (url, next_params)
            if next_request is None:
                raise AdapterError("SAP SuccessFactors response did not expose the next page contract")

        if next_request is not None and page_index + 1 < max_pages:
            current_offset = offset + page.page_size
            time.sleep(PAGE_DELAY_SECONDS)

    if next_request is not None and metadata["pages_fetched"] >= max_pages:
        metadata["stop_reason"] = "maximum_pages_reached"
    elif metadata["stop_reason"] == "not_started" and len(jobs) >= MAX_RESULTS:
        metadata["stop_reason"] = "maximum_results_reached"
    return jobs


def validate_target(target: CompanyTarget) -> None:
    missing = [
        name
        for name, value in (
            ("careers_url", target.careers_url),
            ("search_url", target.search_url),
            ("endpoint_variant", target.endpoint_variant),
        )
        if not str(value or "").strip()
    ]
    if missing:
        raise AdapterError(f"SAP SuccessFactors target is missing {', '.join(missing)}")
    if target.endpoint_variant not in SUPPORTED_VARIANTS:
        raise AdapterError(
            f"SAP SuccessFactors endpoint_variant must be one of {sorted(SUPPORTED_VARIANTS)}"
        )

    search = urlparse(str(target.search_url))
    if (
        search.scheme != "https"
        or not search.hostname
        or not search.path.endswith(SEARCH_PATH_SUFFIX)
        or search.query
        or search.fragment
    ):
        raise AdapterError("SAP SuccessFactors search_url must be an observed HTTPS /search/ URL")
    careers = urlparse(str(target.careers_url))
    if careers.scheme != "https" or not careers.hostname:
        raise AdapterError("SAP SuccessFactors careers_url must be an official HTTPS URL")
    if target.endpoint_variant == COLUMNIZED_VARIANT:
        endpoint = urlparse(str(target.endpoint_url or ""))
        if (
            endpoint.scheme != "https"
            or (endpoint.hostname or "").casefold() != (search.hostname or "").casefold()
            or endpoint.path != COLUMNIZED_ENDPOINT
            or endpoint.query
            or endpoint.fragment
        ):
            raise AdapterError("SAP SuccessFactors columnized endpoint_url must be the matching public jobs service")
        if not str(target.identity_regex or "").strip():
            raise AdapterError("SAP SuccessFactors columnized target requires identity_regex")
        try:
            re.compile(str(target.identity_regex), re.I)
        except re.error as exc:
            raise AdapterError(f"SAP SuccessFactors identity_regex is invalid: {exc}") from exc
        locale = str(target.locale or "")
        if not re.fullmatch(r"[a-z]{2}_[A-Z]{2}", locale):
            raise AdapterError("SAP SuccessFactors columnized target requires an ll_CC locale")
        if int(target.page_size) != COLUMNIZED_PAGE_SIZE:
            raise AdapterError(f"SAP SuccessFactors columnized page_size must be {COLUMNIZED_PAGE_SIZE}")
        if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
            raise AdapterError(f"SAP SuccessFactors max_jobs must be between 1 and {MAX_RESULTS}")
        allowed = {str(value).strip().casefold().rstrip(".") for value in target.allowed_apply_hosts}
        if allowed != {(search.hostname or "").casefold()}:
            raise AdapterError("SAP SuccessFactors columnized allowed_apply_hosts must contain only the search host")


def _fetch_columnized_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int,
) -> List[JobRecord]:
    """Use the public JSON service defined by the standard columnized RMK client."""
    identity_response = _get_page(
        session, target, str(target.search_url), None, 0, timeout
    )
    identity_html = str(getattr(identity_response, "text", "") or "")
    if not re.search(str(target.identity_regex), identity_html, re.I):
        raise AdapterError("SAP SuccessFactors columnized tenant identity did not match identity_regex")
    if "BS3ColumnizedSearch" not in identity_html or "j2w.searchManager" not in identity_html:
        raise AdapterError("SAP SuccessFactors response is not the standard columnized RMK contract")
    tokens = set(re.findall(r'var\s+CSRFToken\s*=\s*"([^"]+)"', identity_html))
    if len(tokens) != 1 or not re.fullmatch(r"[0-9a-fA-F-]{36}", next(iter(tokens), "")):
        raise AdapterError("SAP SuccessFactors columnized page has no unambiguous CSRF token")
    token = next(iter(tokens))
    search = urlparse(str(target.search_url))
    origin = f"{search.scheme}://{search.netloc}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": origin,
        "Referer": str(target.search_url),
        "User-Agent": HEADERS["User-Agent"],
        "X-CSRF-Token": token,
    }
    limit = int(target.max_jobs)
    max_pages = int(target.max_pages)
    jobs: list[JobRecord] = []
    seen: set[str] = set()
    seen_fingerprints: dict[str, str] = {}
    observed_total: Optional[int] = None
    metadata: dict[str, Any] = {
        "endpoint_variant": COLUMNIZED_VARIANT,
        "request_method": "POST",
        "page_size": COLUMNIZED_PAGE_SIZE,
        "maximum_pages": max_pages,
        "maximum_results": limit,
        "pages": [],
        "jobs_normalized": 0,
        "stop_reason": "not_started",
    }
    setattr(session, "sap_successfactors_pagination", metadata)

    for page_number in range(max_pages):
        if len(jobs) >= limit:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        response = session.post(
            str(target.endpoint_url),
            headers=headers,
            json={
                "keywords": "",
                "locale": str(target.locale),
                "location": "",
                "pageNumber": page_number,
                "sortBy": "recent",
            },
            timeout=timeout,
        )
        payload = _columnized_json(response, target, page_number)
        rows = payload["jobSearchResult"]
        total = payload["totalJobs"]
        if observed_total is None:
            observed_total = total
        elif total != observed_total:
            raise AdapterError("SAP SuccessFactors total job count changed during pagination")
        offset = page_number * COLUMNIZED_PAGE_SIZE
        if total > offset and not rows:
            raise AdapterError("SAP SuccessFactors columnized results ended before the reported total")

        page_new_ids = 0
        for envelope in rows:
            row = envelope.get("response") if isinstance(envelope, Mapping) else None
            if not isinstance(row, Mapping):
                raise AdapterError("SAP SuccessFactors columnized result envelope is malformed")
            external_id = str(row.get("id") or "").strip()
            if not re.fullmatch(r"[0-9]{1,18}", external_id):
                raise AdapterError("SAP SuccessFactors columnized job has no stable numeric ID")
            fingerprint = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            if external_id in seen:
                if seen_fingerprints.get(external_id) != fingerprint:
                    raise AdapterError(f"SAP SuccessFactors conflicting repeated stable job ID: {external_id}")
                metadata.setdefault("duplicate_ids_skipped", []).append(external_id)
                continue
            title = _clean_text(row.get("unifiedStandardTitle"))
            url_title = unescape(str(row.get("unifiedUrlTitle") or "").strip())
            locales = row.get("supportedLocales")
            if (
                not title
                or not url_title
                or len(url_title) > 240
                or any(character in url_title for character in "/?#\\")
                or not isinstance(locales, list)
                or str(target.locale) not in locales
            ):
                raise AdapterError("SAP SuccessFactors columnized job is missing title, URL title, or locale")
            detail_url = (
                f"{origin}/job/{quote(unquote(url_title), safe='._~-')}/"
                f"{external_id}-{target.locale}"
            )
            detail_response = _get_page(
                session, target, detail_url, None, offset, timeout
            )
            job = _normalize_columnized_detail(
                row, detail_response, target, external_id, title, detail_url
            )
            jobs.append(job)
            seen.add(external_id)
            seen_fingerprints[external_id] = fingerprint
            page_new_ids += 1
            if len(jobs) >= limit:
                break
        if rows and page_new_ids == 0:
            raise AdapterError("SAP SuccessFactors columnized pagination repeated a complete page")
        metadata["pages"].append(
            {"page_number": page_number, "offset": offset, "jobs_returned": len(rows)}
        )
        metadata["jobs_normalized"] = len(jobs)
        if offset + len(rows) >= total:
            metadata["stop_reason"] = "total_jobs_reached"
            break
    else:
        metadata["stop_reason"] = "maximum_pages_reached"
    metadata["total_available"] = observed_total
    jobs.sort(key=lambda job: (job.updated_at, job.job_id), reverse=True)
    return jobs


def _columnized_json(response: Any, target: CompanyTarget, page_number: int) -> Mapping[str, Any]:
    if response.status_code != 200:
        raise AdapterError(
            f"SAP SuccessFactors HTTP {response.status_code} for {target.name} page={page_number}"
        )
    if len(bytes(getattr(response, "content", b"") or b"")) > int(target.max_response_bytes):
        raise AdapterError("SAP SuccessFactors columnized response exceeded max_response_bytes")
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and "json" not in content_type.casefold():
        raise AdapterError("SAP SuccessFactors columnized response was not JSON")
    try:
        payload = response.json()
    except (ValueError, requests.RequestException) as exc:
        raise AdapterError("SAP SuccessFactors columnized response was not valid JSON") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobSearchResult"), list):
        raise AdapterError("SAP SuccessFactors columnized response is malformed")
    total = payload.get("totalJobs")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("SAP SuccessFactors columnized totalJobs must be non-negative")
    if len(payload["jobSearchResult"]) > COLUMNIZED_PAGE_SIZE:
        raise AdapterError("SAP SuccessFactors columnized page exceeded the observed page size")
    return payload


def _normalize_columnized_detail(
    row: Mapping[str, Any],
    response: Any,
    target: CompanyTarget,
    external_id: str,
    title: str,
    detail_url: str,
) -> JobRecord:
    html = str(getattr(response, "text", "") or "")
    if not re.search(str(target.identity_regex), html, re.I):
        raise AdapterError("SAP SuccessFactors detail tenant identity did not match identity_regex")
    parser = _DomParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        raise AdapterError(f"SAP SuccessFactors detail returned malformed HTML: {exc}") from exc
    canonical_nodes = [
        node for node in _walk(parser.root)
        if node.tag == "link" and node.attrs.get("rel", "").casefold() == "canonical"
    ]
    if len(canonical_nodes) != 1:
        raise AdapterError("SAP SuccessFactors detail has no unambiguous canonical URL")
    canonical = urlparse(urljoin(detail_url, canonical_nodes[0].attrs.get("href", "")))
    expected = urlparse(detail_url)
    if (
        canonical.scheme != "https"
        or (canonical.hostname or "").casefold() != (expected.hostname or "").casefold()
        or not canonical.path.rstrip("/").endswith(f"/{external_id}-{target.locale}")
    ):
        raise AdapterError("SAP SuccessFactors detail canonical URL did not match the job ID")
    og_titles = [
        node.attrs.get("content", "") for node in _walk(parser.root)
        if node.tag == "meta" and node.attrs.get("property", "").casefold() == "og:title"
    ]
    if len(og_titles) != 1 or _clean_text(og_titles[0]) != title:
        raise AdapterError("SAP SuccessFactors detail title did not match the listing")
    apply_matches = re.findall(r"j2w\.Apply\.init\s*\(\s*\{[^}]*jobID\s*:\s*([0-9]+)", html, re.S)
    if apply_matches != [external_id]:
        raise AdapterError("SAP SuccessFactors detail has no official job-specific apply action")

    detail_fields: dict[str, list[str]] = {}
    for node in _walk(parser.root):
        if node.tag != "li":
            continue
        text = _node_text(node)
        match = re.match(r"^([A-Za-z#%][A-Za-z0-9 #&/%-]{1,40}):\s*(.+)$", text)
        if match:
            detail_fields.setdefault(match.group(1).casefold(), []).append(_clean_text(match.group(2)))
    req_ids = _exact_numeric_ids(
        detail_fields.get("req id", [])
        + detail_fields.get("requisition id", [])
    )
    if not req_ids:
        req_ids = _unique_strings(
            re.findall(r"\bRequisition\s+ID:\s*([0-9]{1,18})\b", _node_text(parser.root), re.I)
        )
    # Some standard RMK detail templates omit the human-readable Req ID field.
    # Canonical URL and j2w.Apply.jobID are already independently required above;
    # when a requisition field is present it remains an additional strict check.
    if req_ids and req_ids != [external_id]:
        raise AdapterError("SAP SuccessFactors detail requisition ID did not match the listing")
    api_locations = _columnized_locations(row.get("jobLocationShort"))
    detail_locations = _unique_strings(detail_fields.get("location", []))
    locations = api_locations or detail_locations
    if not locations:
        raise AdapterError("SAP SuccessFactors columnized job is missing a stable location")
    departments = _unique_strings(detail_fields.get("department", []))
    description_nodes = [
        node for node in _walk(parser.root)
        if node.attrs.get("itemprop", "").casefold() == "description"
    ]
    description = _node_text(description_nodes[0]) if len(description_nodes) == 1 else None
    host = (urlparse(str(target.search_url)).hostname or "").casefold()
    posting_date = _normalize_posting_date(row.get("unifiedStandardStart"))
    return JobRecord(
        job_id=f"sap_successfactors:{host}:{external_id}",
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=detail_url,
        updated_at=posting_date,
        source=f"sap_successfactors:{host}",
        departments=departments,
        offices=locations,
        description=description,
        raw_metadata={
            "platform": "sap_successfactors",
            "endpoint_variant": COLUMNIZED_VARIANT,
            "external_job_id": external_id,
            "detail_url": detail_url,
            "apply_url": detail_url,
            "fields": detail_fields,
        },
    )


def _columnized_locations(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AdapterError("SAP SuccessFactors columnized location field is malformed")
    locations: list[str] = []
    for item in value:
        raw = item if isinstance(item, str) else item.get("value") if isinstance(item, Mapping) else None
        if raw is None:
            continue
        text = _clean_text(_html_to_text(raw))
        if text:
            locations.append(text)
    return _unique_strings(locations)


def _exact_numeric_ids(values: Iterable[Any]) -> list[str]:
    """Keep only complete RMK identifiers from possibly nested field wrappers.

    Some columnized detail templates nest the real field list inside another
    ``li``.  The outer node begins with ``Req ID`` but its text also contains
    every following field, while the leaf node contains the exact ID.  Treating
    the wrapper as a second identifier creates a false mismatch.  Requiring the
    entire extracted value to be one numeric ID preserves strict identity
    validation and ignores only presentation containers.
    """
    identifiers: list[str] = []
    for value in values:
        candidate = _clean_text(value)
        if re.fullmatch(r"[0-9]{1,18}", candidate):
            identifiers.append(candidate)
    return _unique_strings(identifiers)


def _get_page(
    session: requests.Session,
    target: CompanyTarget,
    url: str,
    params: Optional[dict[str, str]],
    offset: int,
    timeout: int,
) -> Any:
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(url, params=params, headers=HEADERS, timeout=timeout)
        except requests.RequestException:
            if attempt + 1 >= MAX_ATTEMPTS:
                raise
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        if response.status_code in TRANSIENT_STATUSES and attempt + 1 < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        if response.status_code != 200:
            raise AdapterError(
                f"SAP SuccessFactors HTTP {response.status_code} for {target.name} offset={offset}"
            )
        content_type = str(getattr(response, "headers", {}).get("content-type", ""))
        if content_type and "html" not in content_type.casefold():
            raise AdapterError("SAP SuccessFactors response was not HTML")
        return response
    raise AdapterError(f"SAP SuccessFactors request attempts exhausted for {target.name}")


@dataclass
class _ParsedPage:
    items: list[dict[str, Any]]
    total: Optional[int]
    page_size: int
    next_request: Optional[tuple[str, Optional[dict[str, str]]]]


def _parse_page(
    response: Any,
    target: CompanyTarget,
    offset: int,
    expected_page_size: Optional[int] = None,
) -> _ParsedPage:
    text = str(getattr(response, "text", "") or "")
    if not text.strip():
        raise AdapterError("SAP SuccessFactors returned an empty HTTP 200 response")
    parser = _DomParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        raise AdapterError(f"SAP SuccessFactors returned malformed HTML: {exc}") from exc

    if target.endpoint_variant == TABLE_VARIANT:
        return _parse_table_page(parser.root, str(target.search_url), offset, expected_page_size)
    return _parse_tile_page(
        parser.root,
        text,
        str(target.search_url),
        offset,
        expected_page_size,
    )


def _parse_table_page(
    root: _Node,
    search_url: str,
    offset: int,
    expected_page_size: Optional[int],
) -> _ParsedPage:
    tables = [node for node in _walk(root) if node.attrs.get("id", "").casefold() == "searchresults"]
    explicit_zero = bool(re.search(r"\b0\s+most\s+recent\s+jobs\b", _node_text(root), re.I))
    if not tables:
        if explicit_zero:
            return _ParsedPage([], 0, PAGE_SIZE, None)
        raise AdapterError("SAP SuccessFactors table response is missing searchresults")

    table = tables[0]
    headers = {
        node.attrs["id"].casefold(): _node_text(node)
        for node in _walk(table)
        if node.tag == "th" and node.attrs.get("id")
    }
    items = [_table_item(row, headers) for row in _walk(table) if "data-row" in row.classes]
    items = [item for item in items if item]
    result_range = _table_result_range(root)
    if result_range is None:
        raise AdapterError("SAP SuccessFactors table response is missing total-result metadata")
    _, result_end, total = result_range
    page_size = expected_page_size or (
        PAGE_SIZE
        if total == 0
        else (len(items) if total <= len(items) else result_end - offset)
    )
    if page_size < 1 or page_size > PAGE_SIZE:
        raise AdapterError("SAP SuccessFactors table response has an unsupported page size")
    if total == 0 and items:
        raise AdapterError("SAP SuccessFactors zero-job response unexpectedly contained jobs")
    if len(items) > page_size:
        raise AdapterError("SAP SuccessFactors table page exceeded the observed page size")

    next_request = None
    wanted_offset = offset + page_size
    for node in _walk(root):
        if node.tag != "a" or not node.attrs.get("href"):
            continue
        candidate = urljoin(search_url, unescape(node.attrs["href"]))
        values = dict(parse_qsl(urlparse(candidate).query, keep_blank_values=True))
        if values.get("startrow") == str(wanted_offset):
            next_request = (candidate, None)
            break
    return _ParsedPage(items, total, page_size, next_request)


def _parse_tile_page(
    root: _Node,
    raw_html: str,
    search_url: str,
    offset: int,
    expected_page_size: Optional[int],
) -> _ParsedPage:
    tiles = [node for node in _walk(root) if node.tag == "li" and "job-tile" in node.classes]
    items = [_tile_item(tile) for tile in tiles]
    items = [item for item in items if item]

    if offset:
        if expected_page_size is None or len(items) > expected_page_size:
            raise AdapterError("SAP SuccessFactors tile fragment exceeded the observed page size")
        return _ParsedPage(items, None, expected_page_size, None)

    lists = [node for node in _walk(root) if node.attrs.get("id", "").casefold() == "job-tile-list"]
    total_match = re.search(r'jobRecordsFound\s*:\s*parseInt\("(\d+)"\)', raw_html)
    size_match = re.search(r'jobRecordsPerPage\s*:\s*parseInt\("(\d+)"\)', raw_html)
    if not lists or not total_match or not size_match:
        raise AdapterError("SAP SuccessFactors tile response is missing result metadata")
    page_size = int(size_match.group(1))
    if not 1 <= page_size <= MAX_TILE_PAGE_SIZE:
        raise AdapterError("SAP SuccessFactors tile response has an unsupported page size")
    if len(items) > page_size:
        raise AdapterError("SAP SuccessFactors tile page exceeded the observed page size")
    total = int(total_match.group(1))
    if total == 0 and items:
        raise AdapterError("SAP SuccessFactors zero-job response unexpectedly contained jobs")
    if total and not items:
        raise AdapterError("SAP SuccessFactors tile response reported jobs without job tiles")

    endpoint_match = re.search(r'apiEndpoint\s*:\s*"([^"]+)"', raw_html)
    query_match = re.search(r'searchQuery\s*:\s*"([^"]*)"', raw_html)
    next_request = None
    if total > len(items):
        if not endpoint_match or endpoint_match.group(1) != TILE_ENDPOINT or not query_match:
            raise AdapterError("SAP SuccessFactors tile response is missing pagination contract")
        query = unescape(query_match.group(1))
        parsed_query = urlparse(query)
        params = dict(parse_qsl(parsed_query.query, keep_blank_values=True))
        params["startrow"] = str(page_size)
        base = urlparse(search_url)
        endpoint_url = urlunparse(base._replace(path=f"/{TILE_ENDPOINT}/", query="", fragment=""))
        next_request = (endpoint_url, params)
    return _ParsedPage(items, total, page_size, next_request)


def _table_item(row: _Node, headers: Mapping[str, str]) -> Optional[dict[str, Any]]:
    link = _first_descendant(row, lambda node: node.tag == "a" and "jobtitle-link" in node.classes)
    if link is None:
        return None
    fields: dict[str, list[str]] = {}
    for cell in row.children:
        if cell.tag != "td":
            continue
        label = ""
        header_id = cell.attrs.get("headers", "").split(" ")[0].casefold()
        if header_id:
            label = headers.get(header_id, "")
        if not label:
            label = _label_from_classes(cell.classes)
        value = _node_text(cell)
        if label and value:
            fields.setdefault(label, []).append(value)
    return {"title": _node_text(link), "href": link.attrs.get("href", ""), "fields": fields}


def _tile_item(tile: _Node) -> Optional[dict[str, Any]]:
    link = _first_descendant(tile, lambda node: node.tag == "a" and "jobtitle-link" in node.classes)
    if link is None:
        return None
    fields: dict[str, list[str]] = {}
    for section in _walk(tile):
        if "section-field" not in section.classes:
            continue
        label_node = _first_descendant(section, lambda node: "section-label" in node.classes)
        label = _node_text(label_node) if label_node else _label_from_classes(section.classes)
        value_nodes = [
            node
            for node in _walk(section)
            if node.attrs.get("id", "").casefold().endswith("-value")
        ]
        value = _node_text(value_nodes[0]) if value_nodes else ""
        if label and value:
            fields.setdefault(label, []).append(value)
    return {"title": _node_text(link), "href": link.attrs.get("href", ""), "fields": fields}


def _normalize_item(item: Mapping[str, Any], target: CompanyTarget) -> JobRecord:
    title = _clean_text(item.get("title"))
    apply_url = _official_apply_url(str(target.search_url), item.get("href"))
    url_identifier = _identifier_from_url(apply_url)
    fields = item.get("fields") if isinstance(item.get("fields"), Mapping) else {}
    normalized_fields = {
        _clean_text(label): _unique_strings(values if isinstance(values, list) else [values])
        for label, values in fields.items()
    }
    external_id = _first_field(normalized_fields, ("requisition id", "job id")) or url_identifier
    if not title or not external_id:
        raise AdapterError("SAP SuccessFactors job is missing title or stable requisition ID")

    locations: list[str] = []
    departments: list[str] = []
    descriptions: list[str] = []
    posting_date = ""
    for label, values in normalized_fields.items():
        key = label.casefold()
        if "location" in key or key in {"city", "city/state", "address"}:
            for value in values:
                locations.extend(re.split(r"\s*[|;]\s*", value))
        if any(token in key for token in ("department", "category", "function", "functional area", "job family")):
            departments.extend(values)
        if "date" in key and not posting_date:
            posting_date = _normalize_posting_date(values[0])
        if "description" in key or "summary" in key:
            descriptions.extend(_html_to_text(value) for value in values)

    locations = _unique_strings(locations)
    departments = _unique_strings(departments)
    host = (urlparse(str(target.search_url)).hostname or "").casefold()
    return JobRecord(
        job_id=f"sap_successfactors:{host}:{external_id}",
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=apply_url,
        updated_at=posting_date,
        source=f"sap_successfactors:{host}",
        departments=departments,
        offices=locations,
        description="\n\n".join(value for value in descriptions if value) or None,
        raw_metadata={
            "platform": "sap_successfactors",
            "endpoint_variant": target.endpoint_variant,
            "external_job_id": external_id,
            "url_job_id": url_identifier,
            "fields": normalized_fields,
        },
    )


def _official_apply_url(search_url: str, href: Any) -> str:
    url = urljoin(search_url, str(href or "").strip())
    search = urlparse(search_url)
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.casefold() != (search.hostname or "").casefold()
        or "/job/" not in parsed.path
        or not _identifier_from_url(url)
    ):
        raise AdapterError("SAP SuccessFactors job has no official stable apply URL")
    return urlunparse(parsed._replace(fragment=""))


def _identifier_from_url(url: str) -> str:
    match = re.search(r"/([0-9]+)/?$", urlparse(url).path)
    return match.group(1) if match else ""


def _table_result_range(root: _Node) -> Optional[tuple[int, int, int]]:
    for node in _walk(root):
        if "paginationlabel" not in node.classes:
            continue
        match = re.search(
            r"Results\s+([0-9][0-9,]*)\s*[–-]\s*([0-9][0-9,]*)\s+of\s+([0-9][0-9,]*)",
            _node_text(node),
            re.I,
        )
        if match:
            return tuple(int(value.replace(",", "")) for value in match.groups())
    if re.search(r"\b0\s+most\s+recent\s+jobs\b", _node_text(root), re.I):
        return (0, 0, 0)
    return None


def _first_field(fields: Mapping[str, list[str]], names: Iterable[str]) -> str:
    wanted = {name.casefold() for name in names}
    for label, values in fields.items():
        if label.casefold() in wanted and values:
            return values[0]
    return ""


def _label_from_classes(classes: Iterable[str]) -> str:
    joined = " ".join(classes).casefold()
    for token, label in (
        ("location", "Location"),
        ("date", "Date"),
        ("department", "Department"),
        ("category", "Category"),
        ("function", "Function"),
        ("description", "Description"),
    ):
        if token in joined:
            return label
    return ""


def _normalize_posting_date(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return ""


def _html_to_text(value: Any) -> str:
    # Keep this aligned with the existing lightweight Atlassian HTML cleanup.
    text = unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:li|p|h[1-6]|div)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    return text.strip()


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _walk(node: _Node) -> Iterable[_Node]:
    for child in node.children:
        yield child
        yield from _walk(child)


def _node_text(node: Optional[_Node]) -> str:
    if node is None:
        return ""
    values = [
        _node_text(value) if isinstance(value, _Node) else str(value)
        for value in node.content
    ]
    return _clean_text(" ".join(values))


def _first_descendant(node: _Node, predicate: Any) -> Optional[_Node]:
    for candidate in _walk(node):
        if predicate(candidate):
            return candidate
    return None
