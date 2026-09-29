"""Anonymous public Avature career-search adapter.

Only the three server-rendered contracts observed in the rollout research are
supported.  The parser is selected by configuration; it contains no company
name branches and deliberately rejects other Avature deployments.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Iterable, List, Mapping, Optional
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

import requests
from lxml import html

from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import (
    AdaptiveListingCompletion,
    PageSizeObservation,
    html_page_has_more,
    record_terminal_evidence,
)
from src.scraper.company_careers.mutable_collection import (
    fetch_with_mutation_restart,
    finish_changed,
    finish_terminal,
    immutable_job_fingerprint,
    initialize_mutable_collection,
    mark_collection_changed,
    record_total_observation,
)


PORTAL_HTML = "avature_portal_html"
RADANCY_HTML = "avature_radancy_html"
AEM_HTML = "avature_aem_html"
SUPPORTED_VARIANTS = {PORTAL_HTML, RADANCY_HTML, AEM_HTML}
MAX_PAGES = 2
MAX_RESULTS = 20
MAX_PAGE_SIZE = 20
MAX_TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 0.5
PAGE_DELAY_SECONDS = 0.2
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "User-Agent": "job-radar/1.0 (read-only public careers scan)",
}
HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    re.I,
)


def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> List[JobRecord]:
    return fetch_with_mutation_restart(
        session, lambda: _fetch_jobs_once(session, target, timeout)
    )


def _fetch_jobs_once(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> List[JobRecord]:
    validate_target(target)
    maximum_pages = max(1, int(target.max_pages))
    if maximum_pages > MAX_PAGES:
        raise AdapterError(f"Avature max_pages cannot exceed {MAX_PAGES}")
    maximum_jobs = max(1, int(target.max_jobs))
    if maximum_jobs > MAX_RESULTS:
        raise AdapterError(f"Avature max_jobs cannot exceed {MAX_RESULTS}")
    configured_page_size = max(1, int(target.page_size))
    page_size = (
        MAX_PAGE_SIZE
        if configured_page_size in {4, 10, 12, 15}
        else configured_page_size
    )
    if page_size > MAX_PAGE_SIZE:
        raise AdapterError(f"Avature page_size cannot exceed {MAX_PAGE_SIZE}")
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
    listing_budget = AdaptiveListingCompletion(
        normal_request_budget=maximum_pages,
        normal_job_budget=maximum_jobs,
        allow_listing_only_second_stage=True,
    )
    metadata = SourceScanMetadata(**{
        "endpoint_variant": target.endpoint_variant,
        "request_method": "GET",
        "page_size": page_size,
        "maximum_pages": maximum_pages,
        "maximum_results": maximum_jobs,
        "total_available": None,
        "pages": [],
        "pages_fetched": 0,
        "jobs_normalized": 0,
        "stop_reason": "not_started",
        "repeated_page": False,
        "zero_job_contract": False,
        "diagnostics": [],
        "warnings": [],
    })
    setattr(session, "avature_pagination", metadata)
    publish_source_scan_metadata(session, metadata)
    listing_budget.record(metadata)
    # Every supported variant uses a reviewed page/offset request contract and
    # proves EOF with a short/empty page even when the HTML has no next link.
    listing_budget.record_terminal_probe_decision(
        metadata,
        page_size,
        requests_completed=0,
        results_observed=0,
        deterministic_terminal_contract=True,
    )
    page_sizes = PageSizeObservation(page_size)
    page_sizes.record(metadata)
    initialize_mutable_collection(metadata)

    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    seen_fingerprints: dict[str, tuple[Any, ...]] = {}
    seen_pages: set[tuple[str, ...]] = set()
    positions_seen = 0
    for page_index in range(listing_budget.hard_request_ceiling):
        effective_jobs = int(
            metadata.get(
                "effective_listing_job_ceiling", listing_budget.normal_job_budget
            )
        )
        if len(jobs) >= effective_jobs:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        params = _page_params(target, page_index, page_size, positions_seen)
        metadata["pages_requested"] += 1
        response = _get_page(session, target, params, timeout)
        items, total, zero_contract, has_more = _parse_page(
            response, target, page_index
        )
        metadata["pages_completed"] += 1
        record_total_observation(
            metadata,
            total,
            offset=positions_seen,
            source_label="Avature",
        )
        next_positions_seen = positions_seen + len(items)
        if (
            total is not None
            and not metadata["collection_changed_during_scan"]
            and next_positions_seen > total
        ):
            raise AdapterError("Avature returned more jobs than its reported total")
        page_sizes.observe(
            len(items),
            terminal=(
                (total is not None and next_positions_seen >= total)
                or (total is None and len(items) < page_size)
            ),
        )
        page_sizes.record(metadata)
        if total is not None:
            listing_budget.record_decision(
                metadata,
                total,
                page_sizes.effective_size,
                requests_completed=int(metadata["pages_completed"]),
                results_observed=next_positions_seen,
            )
        if total is None and has_more is not None:
            record_terminal_evidence(
                metadata,
                "explicit_has_more",
                has_more,
                has_more=has_more,
                terminal_page_observed=not has_more,
            )
            listing_budget.record_terminal_probe_decision(
                metadata,
                max(1, len(items)),
                requests_completed=int(metadata["pages_completed"]),
                results_observed=len(jobs) + len(items),
                deterministic_terminal_contract=True,
            )
        effective_jobs = int(metadata["effective_listing_job_ceiling"])

        identities = tuple(
            sorted(_item_identity(item, target.endpoint_variant) for item in items)
        )
        page_record = {
            "page": page_index + 1,
            "params": params,
            "url": getattr(response, "url", str(target.search_url)),
            "jobs_returned": len(items),
            "jobs_normalized": 0,
        }
        metadata["pages"].append(page_record)
        metadata["pages_fetched"] = len(metadata["pages"])
        if items and identities in seen_pages:
            metadata["repeated_page"] = True
            metadata["stop_reason"] = "repeated_page"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("Avature pagination repeated a complete page")
        seen_pages.add(identities)

        parsed_on_page = 0
        new_ids = 0
        for item in items:
            try:
                job = _normalize_item(item, target)
            except AdapterError as exc:
                metadata["diagnostics"].append({"page": page_index + 1, "error": str(exc)})
                continue
            parsed_on_page += 1
            if job.job_id in seen_ids:
                if seen_fingerprints[job.job_id] != immutable_job_fingerprint(job):
                    metadata["stop_reason"] = "conflicting_repeated_job_id"
                    metadata["scan_completeness_reason"] = "pagination_failure"
                    raise AdapterError(
                        f"Avature conflicting repeated stable job ID: {job.job_id}"
                    )
                mark_collection_changed(
                    metadata, offset=positions_seen, overlap_count=1
                )
                continue
            seen_ids.add(job.job_id)
            seen_fingerprints[job.job_id] = immutable_job_fingerprint(job)
            jobs.append(job)
            new_ids += 1
            if len(jobs) >= effective_jobs:
                break
        if items and parsed_on_page == 0:
            raise AdapterError("Avature page contained jobs but none were valid")
        page_record["jobs_normalized"] = new_ids
        metadata["jobs_normalized"] = len(jobs)
        positions_seen = next_positions_seen

        if page_index == 0 and not items:
            if not zero_contract:
                raise AdapterError("Avature HTTP 200 response did not prove an empty jobs collection")
            metadata["zero_job_contract"] = True
            finish_terminal(
                metadata,
                complete_reason="validated_zero_jobs",
                stop_reason="validated_zero_jobs",
            )
            break
        if not items:
            if metadata["collection_changed_during_scan"]:
                finish_changed(metadata, terminal_evidence="empty_terminal_page")
            elif has_more is True:
                metadata["stop_reason"] = "empty_page_with_next_page"
                metadata["scan_completeness_reason"] = "pagination_failure"
            elif total is None:
                metadata["total_available"] = len(jobs)
                finish_terminal(
                    metadata,
                    complete_reason="natural_terminal_page",
                    stop_reason="natural_terminal_empty_page",
                )
            else:
                metadata["stop_reason"] = "empty_page_before_reported_total"
                metadata["scan_completeness_reason"] = "pagination_failure"
            break
        if new_ids == 0:
            metadata["stop_reason"] = "no_new_stable_ids"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("Avature pagination returned no new stable job IDs")
        if total is not None and len(jobs) == total:
            finish_terminal(metadata, complete_reason="reported_total_reached")
            break
        if has_more is False:
            metadata["total_available"] = len(jobs)
            finish_terminal(
                metadata,
                complete_reason="explicit_next_page_exhausted",
            )
            break
        if len(items) < page_size and total is None and has_more is None:
            if metadata["collection_changed_during_scan"]:
                finish_changed(metadata, terminal_evidence="short_terminal_page")
            else:
                metadata["total_available"] = len(jobs)
                finish_terminal(
                    metadata,
                    complete_reason="natural_terminal_page",
                    stop_reason="natural_terminal_short_page",
                )
            break
        if len(jobs) >= effective_jobs:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        if page_index + 1 >= int(metadata["effective_listing_request_ceiling"]):
            metadata["stop_reason"] = "maximum_pages_reached"
            break
        if page_index + 1 < listing_budget.hard_request_ceiling:
            time.sleep(PAGE_DELAY_SECONDS)
    else:
        metadata["stop_reason"] = "maximum_pages_reached"
    if metadata["stop_reason"] in {"maximum_results_reached", "maximum_pages_reached"}:
        metadata["safety_limit_reached"] = True
        if metadata["collection_changed_during_scan"]:
            finish_changed(metadata, terminal_evidence=metadata["stop_reason"])
        else:
            metadata["scan_completeness_reason"] = "safety_limit_reached"
    return jobs


def validate_target(target: CompanyTarget) -> None:
    missing = [
        name
        for name, value in (
            ("search_url", target.search_url),
            ("endpoint_variant", target.endpoint_variant),
            ("board_id", target.board_id),
            ("allowed_apply_hosts", target.allowed_apply_hosts),
        )
        if not value
    ]
    if missing:
        raise AdapterError(f"Avature target is missing {', '.join(missing)}")
    if target.endpoint_variant not in SUPPORTED_VARIANTS:
        raise AdapterError(
            f"Avature endpoint_variant must be one of {sorted(SUPPORTED_VARIANTS)}"
        )
    parsed = urlparse(str(target.search_url))
    if parsed.scheme != "https" or not parsed.hostname or parsed.fragment:
        raise AdapterError("Avature search_url must be an observed public HTTPS URL")
    if any(token in parsed.path.casefold() for token in ("login", "signin", "session")):
        raise AdapterError("Avature search_url cannot be an authentication or session URL")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", str(target.board_id)):
        raise AdapterError("Avature board_id is invalid")
    for host in target.allowed_apply_hosts:
        if not HOST_RE.fullmatch(str(host)):
            raise AdapterError("Avature allowed_apply_hosts contains an invalid host")
    if not 1 <= int(target.page_size) <= MAX_PAGE_SIZE:
        raise AdapterError(f"Avature page_size must be between 1 and {MAX_PAGE_SIZE}")


def _page_params(
    target: CompanyTarget,
    page_index: int,
    page_size: int,
    offset: Optional[int] = None,
) -> dict[str, str]:
    effective_offset = page_index * page_size if offset is None else offset
    if target.endpoint_variant == PORTAL_HTML:
        return {
            "jobRecordsPerPage": str(page_size),
            "jobOffset": str(effective_offset),
        }
    if target.endpoint_variant == RADANCY_HTML:
        return {"p": str(page_index + 1)}
    return {
        "ref": "search",
        "search": "getAllJobs",
        "start": str(effective_offset),
        "rows": str(page_size),
    }


def _get_page(
    session: requests.Session,
    target: CompanyTarget,
    params: Mapping[str, str],
    timeout: int,
) -> Any:
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(
                str(target.search_url), params=dict(params), headers=HEADERS, timeout=timeout
            )
        except requests.RequestException:
            if attempt + 1 >= MAX_ATTEMPTS:
                raise
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        status = getattr(response, "status_code", None)
        if status in TRANSIENT_STATUSES and attempt + 1 < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        if status in {401, 407}:
            raise AdapterError(f"Avature authentication required (HTTP {status})")
        if status in {403, 429, 451}:
            raise AdapterError(f"Avature endpoint access was blocked (HTTP {status})")
        if status != 200:
            raise AdapterError(f"Avature HTTP {status} for {target.name}")
        return response
    raise AdapterError(f"Avature request attempts exhausted for {target.name}")


def _parse_page(
    response: Any,
    target: CompanyTarget,
    page_index: int = 0,
) -> tuple[list[Any], Optional[int], bool, Optional[bool]]:
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    text = str(getattr(response, "text", "") or "")
    _reject_bad_success(text, content_type)
    try:
        root = html.fromstring(text)
    except (ValueError, TypeError) as exc:
        raise AdapterError(f"Avature returned malformed HTML: {exc}") from exc

    if target.endpoint_variant == PORTAL_HTML:
        items = root.xpath("//article[contains(@class, 'article--result') or contains(@class, 'article--card')]")
        zero = _zero_text(root)
        total = _integer_attr(root, "data-total-job-results")
    elif target.endpoint_variant == RADANCY_HTML:
        items = root.xpath(
            "//li[contains(@class, 'search-results-list__list-item') "
            "or contains(concat(' ', normalize-space(@class), ' '), ' job-card ') "
            "or @data-intuit-jobid]"
        )
        items = _dedupe_items(
            [item for item in items if item.xpath(".//a[@data-job-id and contains(@href, '/job/')]")],
            target.endpoint_variant,
        )
        total = _integer_attr(root, "data-total-job-results")
        zero = total == 0 or _zero_text(root)
    else:
        items = root.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' job-search-tile ')]")
        # Nested tile-body nodes are excluded by the exact class-token query.
        items = [item for item in items if item.xpath(".//a[contains(@class, 'job-search-tile__url')]")]
        total = _integer_attr(root, "data-total-results")
        zero = total == 0 or _zero_text(root)
    has_more = (
        (page_index + 1) * max(1, int(target.page_size)) < total
        if total is not None
        else html_page_has_more(root)
    )
    return items, total, zero, has_more


def _reject_bad_success(text: str, content_type: str) -> None:
    lowered = text.casefold()
    if not text.strip():
        raise AdapterError("Avature returned an empty HTTP 200 response")
    if "html" not in content_type.casefold() and not text.lstrip().startswith("<"):
        raise AdapterError("Avature returned a non-HTML response")
    has_contract = any(
        token in lowered
        for token in (
            "article--result",
            "article--card",
            "search-results-list__list-item",
            "job-search-tile__url",
            "data-intuit-jobid",
        )
    )
    if not has_contract and any(
        token in lowered
        for token in ("captcha", "access denied", "request blocked", "cloudflare challenge")
    ):
        raise AdapterError("Avature returned a blocked page")
    if not has_contract and any(
        token in lowered for token in ("enable javascript to continue", "javascript is required")
    ):
        raise AdapterError("Avature browser required by successful HTML response")
    if not has_contract and (
        "session required" in lowered
        or re.search(r"<title[^>]*>[^<]*(?:session expired|session required)", lowered)
    ):
        raise AdapterError("Avature session required by successful HTML response")
    if not has_contract and re.search(r"<title[^>]*>\s*(?:sign in|log in|login)", lowered):
        raise AdapterError("Avature authentication required by successful HTML response")


def _normalize_item(item: Any, target: CompanyTarget) -> JobRecord:
    if target.endpoint_variant == PORTAL_HTML:
        return _normalize_portal(item, target)
    if target.endpoint_variant == RADANCY_HTML:
        return _normalize_radancy(item, target)
    return _normalize_aem(item, target)


def _normalize_portal(item: Any, target: CompanyTarget) -> JobRecord:
    links = item.xpath(".//h3//a[contains(@href, 'JobDetail')]")
    if not links:
        raise AdapterError("Avature portal job is missing its detail link")
    link = links[0]
    title = _text(link)
    url, external_id = _official_url(link.get("href"), target)
    locations = _texts_by_class(item, ("list-item-location", "list-item-jobCity", "list-item-jobState", "list-item-jobCountry"))
    if not locations:
        locations = _label_values(item, "Location")
    if not locations:
        subtitle_spans = item.xpath(".//*[contains(@class, 'article__header__text__subtitle')]/span")
        locations = [
            _text(span)
            for span in subtitle_spans
            if _text(span) and not re.search(r"(?i)^(?:req\s*#|posted)", _text(span))
        ][:1]
    categories = _texts_by_class(item, ("list-item-family", "list-item-category"))
    categories.extend(_label_values(item, "Function"))
    if not categories:
        categories = [_text(value) for value in item.xpath(".//span[contains(@class, 'paragraph')]") if _text(value)]
    posted = ""
    posted_values = _texts_by_class(item, ("list-item-posted",))
    if not posted_values:
        posted_values = [
            _text(span)
            for span in item.xpath(".//*[contains(@class, 'article__header__text__subtitle')]/span")
            if re.search(r"(?i)^posted", _text(span))
        ]
    if posted_values:
        posted = _normalize_date(re.sub(r"(?i)^posted\s*:?[ ]*", "", posted_values[0]))
    return _job(target, external_id, title, url, locations, posted, categories, item)


def _normalize_radancy(item: Any, target: CompanyTarget) -> JobRecord:
    links = item.xpath(".//a[@data-job-id and contains(@href, '/job/')]")
    if not links:
        raise AdapterError("Avature Radancy job is missing its detail link")
    link = links[0]
    url, path_id = _official_url(link.get("href"), target)
    external_id = _text_value(link.get("data-job-id")) or path_id
    title_nodes = link.xpath(".//h2")
    title = _text(title_nodes[0]) if title_nodes else _text_value(link.get("data-title"))
    locations = _texts_by_class(link, ("job-location", "location"))
    categories = _texts_by_class(link, ("category",))
    if not categories:
        raw = item.get("data-category")
        categories = [part.strip() for part in str(raw or "").split("--") if part.strip()]
    posted_values = _texts_by_class(link, ("job-date-posted",))
    posted = _normalize_date(re.sub(r"(?i)^posted\s*:?[ ]*", "", posted_values[0])) if posted_values else ""
    return _job(target, external_id, title, url, locations, posted, categories, item)


def _normalize_aem(item: Any, target: CompanyTarget) -> JobRecord:
    links = item.xpath(".//a[contains(@class, 'job-search-tile__url')]")
    if not links:
        raise AdapterError("Avature AEM job is missing its detail link")
    link = links[0]
    url, external_id = _official_url(link.get("href"), target)
    body_paragraphs = [_text(node) for node in item.xpath(".//*[contains(@class, 'job-search-tile__body')][1]/p")]
    details = " ".join(_text(node) for node in item.xpath(".//*[contains(@class, 'job-search-tile__detail')]"))
    location_match = re.search(r"(?i)location\s+(.+)$", details)
    posted_match = re.search(r"(?i)posted\s+(\d{1,2}/\d{1,2}/\d{4})", details)
    locations = [location_match.group(1).strip()] if location_match else []
    categories = [value for value in body_paragraphs[:2] if value]
    posted = _normalize_date(posted_match.group(1)) if posted_match else ""
    return _job(target, external_id, _text(link), url, locations, posted, categories, item)


def _job(
    target: CompanyTarget,
    external_id: str,
    title: str,
    url: str,
    locations: Iterable[str],
    posted: str,
    categories: Iterable[str],
    item: Any,
) -> JobRecord:
    locations = _unique(locations)
    categories = _unique(categories)
    if not title or not external_id:
        raise AdapterError("Avature job is missing title or stable identifier")
    host = (urlparse(str(target.search_url)).hostname or "").casefold()
    work_modes = [value for value in locations if re.search(r"(?i)remote|hybrid", value)]
    return JobRecord(
        job_id=f"avature:{host}:{external_id}",
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=url,
        updated_at=posted,
        source=f"avature:{host}:{target.board_id}",
        departments=categories,
        offices=locations,
        raw_metadata={
            "platform": "avature",
            "endpoint_variant": target.endpoint_variant,
            "board_id": target.board_id,
            "external_job_id": external_id,
            "work_modes": work_modes,
            "listing_html": html.tostring(item, encoding="unicode")[:10000],
        },
    )


def _official_url(value: Any, target: CompanyTarget) -> tuple[str, str]:
    url = urljoin(str(target.search_url), str(value or "").strip())
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not _allowed_host(host, target.allowed_apply_hosts):
        raise AdapterError("Avature job has no official allowed-host apply URL")
    query_id = parse_qs(parsed.query).get("jobId", [""])[0]
    aem_match = re.search(r"/job-detail/(\d+)(?:/|$)", parsed.path, re.I)
    path_matches = re.findall(r"/(\d+)(?:/)?$", parsed.path)
    external_id = query_id or (aem_match.group(1) if aem_match else "") or (
        path_matches[-1] if path_matches else ""
    )
    if not external_id:
        raise AdapterError("Avature apply URL has no stable job identifier")
    return urlunparse(parsed._replace(fragment="")), external_id


def _allowed_host(host: str, allowed: Iterable[str]) -> bool:
    return any(host == value.casefold() or host.endswith("." + value.casefold()) for value in allowed)


def _item_identity(item: Any, variant: Optional[str]) -> str:
    if variant == AEM_HTML:
        values = item.xpath(".//a[contains(@class, 'job-search-tile__url')]/@href")
    else:
        values = item.xpath(".//a[@data-job-id]/@data-job-id | .//a[contains(@href, 'JobDetail')]/@href")
    return _text_value(values[0]) if values else _text(item)[:100]


def _dedupe_items(items: Iterable[Any], variant: Optional[str]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for item in items:
        identity = _item_identity(item, variant)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(item)
    return result


def _integer_attr(root: Any, name: str) -> Optional[int]:
    values = root.xpath(f"//*[@{name}]/@{name}")
    if not values:
        return None
    try:
        value = int(values[0])
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"Avature {name} must be a non-negative integer") from exc
    if value < 0:
        raise AdapterError(f"Avature {name} must be a non-negative integer")
    return value


def _zero_text(root: Any) -> bool:
    text = " ".join(root.text_content().split()).casefold()
    return bool(re.search(r"\b(?:0|no)\s+(?:jobs|results|positions)(?:\s+found)?\b", text))


def _texts_by_class(item: Any, names: Iterable[str]) -> list[str]:
    result: list[str] = []
    for name in names:
        for node in item.xpath(f".//*[contains(concat(' ', normalize-space(@class), ' '), ' {name} ')]"):
            value = _text(node)
            value = re.sub(r"(?i)^(?:category|posted|job id)\s*:\s*", "", value)
            if value:
                result.append(value)
    return _unique(result)


def _label_values(item: Any, label: str) -> list[str]:
    values: list[str] = []
    for strong in item.xpath(".//strong"):
        if _text(strong).rstrip(":").casefold() != label.casefold():
            continue
        parent = strong.getparent()
        siblings = parent.xpath("./*[not(self::strong)]") if parent is not None else []
        values.extend(_text(node) for node in siblings if _text(node))
    return _unique(values)


def _normalize_date(value: str) -> str:
    text = _text_value(value)
    if not text:
        return ""
    for fmt in ("%m/%d/%Y", "%b %d, %Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def _text(node: Any) -> str:
    return " ".join(node.text_content().split())


def _text_value(value: Any) -> str:
    return " ".join(str(value or "").split())


def _unique(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _text_value(value).strip(" |")
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result
