"""Phenom server-rendered public career-search adapter.

This adapter intentionally supports only the observed ``eagerLoadRefineSearch``
contract embedded in official Phenom search-result pages.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any, Iterable, List, Mapping
from urllib.parse import parse_qs, quote, urljoin, urlparse, urlunparse

import requests

from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import AdaptiveListingCompletion


EMBEDDED_REFINE_SEARCH_VARIANT = "embedded_refine_search"
SUPPORTED_VARIANTS = {EMBEDDED_REFINE_SEARCH_VARIANT}
PAGE_SIZE = 10
MAX_PAGES = 50
MAX_RESULTS = PAGE_SIZE * MAX_PAGES
MAX_TIMEOUT_SECONDS = 30
CONNECT_TIMEOUT_SECONDS = 5
MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 0.5
MAX_RETRY_AFTER_SECONDS = 5.0
PAGE_DELAY_SECONDS = 0.2
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
REF_NUM_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,63}$", re.I)
LOCALE_RE = re.compile(r"^[a-z]{2,8}_[a-z]{2,12}$", re.I)
HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    re.I,
)
SEARCH_PATH_RE = re.compile(r"(?:^|/)search-results/?$", re.I)
DDO_MARKER = "phApp.ddo ="
CONFIG_RE = re.compile(
    r"var\s+phApp\s*=\s*phApp\s*\|\|\s*(\{.*?\})\s*;\s*phApp\.ddo\s*=",
    re.S,
)
HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "User-Agent": "job-radar/1.0 (read-only public careers scan)",
}


def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> List[JobRecord]:
    validate_target(target)
    max_pages = max(1, int(target.max_pages))
    if max_pages > MAX_PAGES:
        raise AdapterError(f"Phenom max_pages cannot exceed {MAX_PAGES}")
    read_timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
    request_timeout = (min(CONNECT_TIMEOUT_SECONDS, read_timeout), read_timeout)
    maximum_results = min(MAX_RESULTS, int(target.max_jobs), max_pages * PAGE_SIZE)
    listing_budget = AdaptiveListingCompletion(
        normal_request_budget=max_pages,
        normal_job_budget=maximum_results,
        allow_listing_only_second_stage=True,
    )
    metadata = SourceScanMetadata(
        endpoint_variant=target.endpoint_variant,
        request_method="GET",
        page_size=PAGE_SIZE,
        maximum_pages=max_pages,
        maximum_results=maximum_results,
        total_available=None,
        jobs_normalized=0,
        offsets=[],
        pages=[],
        stop_reason="not_started",
        diagnostics=[],
    )
    listing_budget.record(metadata)
    setattr(session, "phenom_pagination", metadata)
    publish_source_scan_metadata(session, metadata)

    jobs: list[JobRecord] = []
    seen_job_ids: set[str] = set()
    seen_page_ids: set[tuple[str, ...]] = set()

    for page_index in range(listing_budget.hard_request_ceiling):
        effective_results = int(
            metadata.get(
                "effective_listing_job_ceiling", listing_budget.normal_job_budget
            )
        )
        if len(jobs) >= effective_results:
            metadata["stop_reason"] = "maximum_results_reached"
            metadata["scan_completeness_reason"] = "safety_limit_reached"
            metadata["safety_limit_reached"] = True
            break
        offset = page_index * PAGE_SIZE
        params = {} if offset == 0 else {"from": str(offset), "s": "1"}
        metadata["pages_requested"] += 1
        try:
            response = _get_page(session, target, params, request_timeout)
            items, total, hits, response_config = _parse_page(response, target, offset)
        except Exception as exc:
            message = str(exc).casefold()
            if metadata["stop_reason"] == "not_started":
                metadata["stop_reason"] = (
                    "malformed_total" if "total" in message else "pagination_failure"
                )
            metadata["scan_completeness_reason"] = metadata["stop_reason"]
            raise

        if metadata["total_available"] is None:
            metadata["total_available"] = total
        elif total != metadata["total_available"]:
            metadata["stop_reason"] = "inconsistent_total"
            metadata["scan_completeness_reason"] = "inconsistent_total"
            raise AdapterError("Phenom total job count changed during pagination")
        listing_budget.record_decision(
            metadata,
            total,
            PAGE_SIZE,
            requests_completed=int(metadata["pages_requested"]),
            results_observed=offset + len(items),
        )
        effective_results = int(metadata["effective_listing_job_ceiling"])

        page_ids = tuple(_item_identity(item) for item in items)
        if items and page_ids in seen_page_ids:
            _record_page(metadata, response, offset, len(items), 0, hits, response_config)
            metadata["pages_completed"] += 1
            metadata["stop_reason"] = "repeated_page"
            metadata["scan_completeness_reason"] = "pagination_failure"
            break
        seen_page_ids.add(page_ids)

        normalized_count = 0
        new_id_count = 0
        for item in items:
            try:
                job = _normalize_item(item, target)
            except AdapterError as exc:
                metadata["diagnostics"].append(
                    {"offset": offset, "error": str(exc), "item": dict(item)}
                )
                continue
            normalized_count += 1
            if job.job_id in seen_job_ids:
                metadata["diagnostics"].append(
                    {"offset": offset, "error": f"Duplicate stable job ID: {job.job_id}"}
                )
                continue
            seen_job_ids.add(job.job_id)
            new_id_count += 1
            jobs.append(job)
            if len(jobs) >= effective_results:
                break

        if items and normalized_count == 0:
            metadata["stop_reason"] = "pagination_failure"
            metadata["scan_completeness_reason"] = "pagination_failure"
            raise AdapterError("Phenom page contained jobs but none were valid")

        _record_page(metadata, response, offset, len(items), new_id_count, hits, response_config)
        metadata["pages_completed"] += 1
        metadata["jobs_normalized"] = len(jobs)

        if total == 0:
            metadata["stop_reason"] = "validated_zero_jobs"
            metadata["scan_complete"] = True
            metadata["scan_completeness_reason"] = "reported_total_reached"
            break
        if items and new_id_count == 0:
            metadata["stop_reason"] = "no_new_stable_ids"
            metadata["scan_completeness_reason"] = "pagination_failure"
            break
        if offset + len(items) >= total:
            metadata["stop_reason"] = "total_jobs_reached"
            metadata["scan_complete"] = True
            metadata["scan_completeness_reason"] = "reported_total_reached"
            break
        if len(items) < PAGE_SIZE:
            metadata["stop_reason"] = "malformed_total"
            metadata["scan_completeness_reason"] = "malformed_total"
            raise AdapterError("Phenom response ended before its reported total job count")
        if len(jobs) >= effective_results:
            metadata["stop_reason"] = "maximum_results_reached"
            metadata["scan_completeness_reason"] = "safety_limit_reached"
            metadata["safety_limit_reached"] = True
            break
        if page_index + 1 >= int(metadata["effective_listing_request_ceiling"]):
            metadata["stop_reason"] = "maximum_pages_reached"
            metadata["scan_completeness_reason"] = "safety_limit_reached"
            metadata["safety_limit_reached"] = True
            break
        if page_index + 1 < listing_budget.hard_request_ceiling:
            time.sleep(PAGE_DELAY_SECONDS)
    else:
        metadata["stop_reason"] = "maximum_pages_reached"
        metadata["scan_completeness_reason"] = "safety_limit_reached"
        metadata["safety_limit_reached"] = True

    if metadata["stop_reason"] == "not_started":
        metadata["stop_reason"] = "maximum_pages_reached"
        metadata["scan_completeness_reason"] = "safety_limit_reached"
        metadata["safety_limit_reached"] = True
    return jobs


def validate_target(target: CompanyTarget) -> None:
    missing = [
        name
        for name, value in (
            ("careers_url", target.careers_url),
            ("search_url", target.search_url),
            ("endpoint_variant", target.endpoint_variant),
            ("ref_num", target.ref_num),
            ("locale", target.locale),
        )
        if not str(value or "").strip()
    ]
    if missing:
        raise AdapterError(f"Phenom target is missing {', '.join(missing)}")
    if target.endpoint_variant not in SUPPORTED_VARIANTS:
        raise AdapterError(
            f"Phenom endpoint_variant must be one of {sorted(SUPPORTED_VARIANTS)}"
        )
    if not REF_NUM_RE.fullmatch(str(target.ref_num).strip()):
        raise AdapterError("Phenom ref_num is invalid")
    if not LOCALE_RE.fullmatch(str(target.locale).strip()):
        raise AdapterError("Phenom locale is invalid")

    careers = urlparse(str(target.careers_url))
    search = urlparse(str(target.search_url))
    if careers.scheme != "https" or not careers.hostname or careers.query or careers.fragment:
        raise AdapterError("Phenom careers_url must be an observed HTTPS URL")
    if (
        search.scheme != "https"
        or not search.hostname
        or search.hostname.casefold() != careers.hostname.casefold()
        or not SEARCH_PATH_RE.search(search.path)
        or search.query
        or search.fragment
    ):
        raise AdapterError(
            "Phenom search_url must be the observed same-host search-results URL"
        )

    hosts = [str(value).strip().casefold() for value in target.allowed_apply_hosts]
    if not hosts:
        raise AdapterError("Phenom target is missing allowed_apply_hosts")
    for host in hosts:
        if not HOST_RE.fullmatch(host):
            raise AdapterError("Phenom allowed_apply_hosts contains an invalid host")

    if target.apply_url_template:
        template = str(target.apply_url_template)
        if template.count("{job_seq_no}") != 1:
            raise AdapterError("Phenom apply_url_template must contain {job_seq_no}")
        parsed_template = urlparse(template.replace("{job_seq_no}", "JOBSEQ"))
        if (
            parsed_template.scheme != "https"
            or not parsed_template.hostname
            or parsed_template.hostname.casefold() != careers.hostname.casefold()
        ):
            raise AdapterError("Phenom apply_url_template must use the official careers host")


def _get_page(
    session: requests.Session,
    target: CompanyTarget,
    params: Mapping[str, str],
    timeout: tuple[int, int],
) -> Any:
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(
                str(target.search_url),
                params=dict(params),
                headers=HTML_HEADERS,
                timeout=timeout,
            )
        except requests.RequestException:
            if attempt + 1 >= MAX_ATTEMPTS:
                raise
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        status = getattr(response, "status_code", None)
        if status in TRANSIENT_STATUSES and attempt + 1 < MAX_ATTEMPTS:
            time.sleep(_retry_delay(response))
            continue
        if status in {401, 407}:
            raise AdapterError(f"Phenom authentication required (HTTP {status})")
        if status in {403, 451}:
            raise AdapterError(f"Phenom endpoint access was blocked (HTTP {status})")
        if status != 200:
            raise AdapterError(
                f"Phenom HTTP {status} for {target.name} from={params.get('from', '0')}"
            )
        return response
    raise AdapterError(f"Phenom request attempts exhausted for {target.name}")


def _retry_delay(response: Any) -> float:
    value = str(getattr(response, "headers", {}).get("Retry-After", "") or "").strip()
    if value:
        try:
            return min(MAX_RETRY_AFTER_SECONDS, max(0.0, float(value)))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                now = datetime.now(retry_at.tzinfo)
                return min(MAX_RETRY_AFTER_SECONDS, max(0.0, (retry_at - now).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    return RETRY_DELAY_SECONDS


def _parse_page(
    response: Any,
    target: CompanyTarget,
    offset: int,
) -> tuple[list[Mapping[str, Any]], int, int, Mapping[str, Any]]:
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    text = str(getattr(response, "text", "") or "")
    if content_type and "html" not in content_type.casefold():
        raise AdapterError("Phenom returned a non-HTML search response")
    try:
        config = _extract_config(text)
        ddo = _extract_json_object(text, DDO_MARKER)
    except (json.JSONDecodeError, ValueError) as exc:
        _raise_unrecognized_html(text, exc)

    _validate_page_identity(config, target)
    search = ddo.get("eagerLoadRefineSearch")
    if not isinstance(search, Mapping):
        raise AdapterError("Phenom response is missing eagerLoadRefineSearch")
    if search.get("status") != 200:
        raise AdapterError("Phenom eagerLoadRefineSearch status must be 200")
    hits = search.get("hits")
    total = search.get("totalHits")
    data = search.get("data")
    if isinstance(hits, bool) or not isinstance(hits, int) or hits < 0:
        raise AdapterError("Phenom hits must be a non-negative integer")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("Phenom totalHits must be a non-negative integer")
    if not isinstance(data, Mapping):
        raise AdapterError("Phenom search response must contain a data object")
    items = data.get("jobs")
    if not isinstance(items, list):
        raise AdapterError("Phenom data.jobs must be a list")
    if any(not isinstance(item, Mapping) for item in items):
        raise AdapterError("Phenom job entries must be JSON objects")
    if len(items) > PAGE_SIZE or hits != len(items):
        raise AdapterError("Phenom hits must match the observed page size and job list")
    if total == 0 and items:
        raise AdapterError("Phenom zero-job response unexpectedly contained jobs")
    if total > offset and not items:
        raise AdapterError("Phenom response ended before its reported total job count")
    return items, total, hits, config


def _extract_config(text: str) -> Mapping[str, Any]:
    match = CONFIG_RE.search(text)
    if not match:
        raise ValueError("missing Phenom page configuration")
    config = json.loads(match.group(1))
    if not isinstance(config, Mapping):
        raise ValueError("Phenom page configuration must be an object")
    return config


def _extract_json_object(text: str, marker: str) -> Mapping[str, Any]:
    marker_index = text.find(marker)
    if marker_index < 0:
        raise ValueError(f"missing {marker.strip()}")
    start = text.find("{", marker_index + len(marker))
    if start < 0:
        raise ValueError(f"missing JSON after {marker.strip()}")
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                value = json.loads(text[start : index + 1])
                if not isinstance(value, Mapping):
                    raise ValueError(f"{marker.strip()} must contain an object")
                return value
    raise ValueError(f"unterminated JSON after {marker.strip()}")


def _raise_unrecognized_html(text: str, error: Exception) -> None:
    lowered = text.casefold()
    if any(token in lowered for token in ("captcha", "access denied", "bot challenge", "request blocked")):
        raise AdapterError("Phenom returned a blocked page") from error
    if any(token in lowered for token in ("consent required", "cookie consent required")):
        raise AdapterError("Phenom returned a consent page") from error
    if any(token in lowered for token in ("sign in required", "login required", "authentication required")):
        raise AdapterError("Phenom authentication required by successful HTML response") from error
    if "session" in lowered and any(
        token in lowered for token in ("initialize", "initialise", "required", "expired")
    ):
        raise AdapterError("Phenom session required by successful HTML response") from error
    if any(token in lowered for token in ("enable javascript", "javascript required")):
        raise AdapterError("Phenom browser required by successful HTML response") from error
    raise AdapterError(f"Phenom returned malformed or unsupported HTML: {error}") from error


def _validate_page_identity(config: Mapping[str, Any], target: CompanyTarget) -> None:
    if _clean_text(config.get("refNum")).casefold() != str(target.ref_num).casefold():
        raise AdapterError("Phenom response refNum does not match the configured tenant")
    if _clean_text(config.get("locale")).casefold() != str(target.locale).casefold():
        raise AdapterError("Phenom response locale does not match the configured locale")
    if _clean_text(config.get("siteType")).casefold() != "external":
        raise AdapterError("Phenom response is not an external careers site")
    if _clean_text(config.get("pageName")).casefold() != "search-results":
        raise AdapterError("Phenom response is not a search-results page")
    search_host = (urlparse(str(target.search_url)).hostname or "").casefold()
    base_host = (urlparse(_clean_text(config.get("baseDomain"))).hostname or "").casefold()
    if base_host != search_host:
        raise AdapterError("Phenom response baseDomain does not match the configured search host")


def _normalize_item(item: Mapping[str, Any], target: CompanyTarget) -> JobRecord:
    title = _clean_text(item.get("title"))
    apply_url = _official_apply_url(item, target)
    external_id = (
        _clean_text(item.get("reqId"))
        or _clean_text(item.get("jobId"))
        or _clean_text(item.get("jobSeqNo"))
        or _url_identifier(apply_url)
    )
    if not title or not external_id:
        raise AdapterError("Phenom job is missing title or a stable identifier")

    locations = _location_values(item)
    work_modes = _work_modes(item)
    location_display = list(locations)
    for mode in work_modes:
        if not any(mode.casefold() in value.casefold() for value in location_display):
            location_display.append(mode)
    metadata_groups = _metadata_groups(item)
    description = _description(item)
    host = (urlparse(str(target.search_url)).hostname or "").casefold()
    ref_num = str(target.ref_num).strip()
    return JobRecord(
        job_id=f"phenom:{host}:{ref_num}:{external_id}",
        title=title,
        company=target.name,
        location=" | ".join(location_display),
        url=apply_url,
        updated_at=_normalize_posting_date(
            item.get("postedDate")
            or item.get("jobUpdatedDate")
            or item.get("jobUpdated")
            or item.get("dateCreated")
        ),
        source=f"phenom:{host}:{ref_num}",
        departments=metadata_groups,
        offices=locations,
        description=description or None,
        raw_metadata={
            "platform": "phenom",
            "endpoint_variant": target.endpoint_variant,
            "ref_num": target.ref_num,
            "locale": target.locale,
            "endpoint_url": target.search_url,
            "external_job_id": external_id,
            "job": dict(item),
        },
    )


def _official_apply_url(item: Mapping[str, Any], target: CompanyTarget) -> str:
    value = _clean_text(item.get("applyUrl"))
    if not value and target.apply_url_template:
        job_seq_no = _clean_text(item.get("jobSeqNo"))
        if job_seq_no:
            value = str(target.apply_url_template).replace(
                "{job_seq_no}", quote(job_seq_no, safe="")
            )
    if not value:
        raise AdapterError("Phenom job has no official apply URL")
    base = str(target.careers_url).rstrip("/") + "/"
    url = urljoin(base, value)
    parsed = urlparse(url)
    allowed = {host.casefold() for host in target.allowed_apply_hosts}
    allowed.add((urlparse(str(target.careers_url)).hostname or "").casefold())
    allowed.add((urlparse(str(target.search_url)).hostname or "").casefold())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.casefold() not in allowed
        or parsed.username
        or parsed.password
    ):
        raise AdapterError("Phenom job apply URL is not on an observed official host")
    return urlunparse(parsed._replace(fragment=""))


def _url_identifier(url: str) -> str:
    parsed = urlparse(url)
    query_id = (parse_qs(parsed.query).get("career_job_req_id") or [""])[0]
    if _clean_text(query_id):
        return _clean_text(query_id)
    match = re.search(r"_([A-Z0-9-]+)/(?:apply)?/?$", parsed.path, re.I)
    return _clean_text(match.group(1)) if match else ""


def _item_identity(item: Mapping[str, Any]) -> str:
    return (
        _clean_text(item.get("reqId"))
        or _clean_text(item.get("jobId"))
        or _clean_text(item.get("jobSeqNo"))
        or _clean_text(item.get("applyUrl"))
    )


def _location_values(item: Mapping[str, Any]) -> list[str]:
    values: list[Any] = [
        item.get("location"),
        item.get("locationName"),
        item.get("cityStateCountry"),
        item.get("cityState"),
    ]
    for field in ("multi_location", "multi_location_array"):
        raw = item.get(field)
        if not isinstance(raw, list):
            continue
        for value in raw:
            if isinstance(value, Mapping):
                values.append(
                    value.get("location")
                    or value.get("cityStateCountry")
                    or value.get("cityState")
                )
            else:
                values.append(value)
    locations: list[str] = []
    for value in _unique_strings(values):
        folded = value.casefold()
        if any(
            folded in existing.casefold() or existing.casefold() in folded
            for existing in locations
        ):
            continue
        locations.append(value)
    return locations


def _work_modes(item: Mapping[str, Any]) -> list[str]:
    values = [
        item.get("remote"),
        item.get("workstyle"),
        item.get("workStyle"),
        item.get("workLocation"),
    ]
    return [
        value
        for value in _unique_strings(values)
        if "remote" in value.casefold() or "hybrid" in value.casefold()
    ]


def _metadata_groups(item: Mapping[str, Any]) -> list[str]:
    values: list[Any] = [
        item.get("department"),
        item.get("departmentName"),
        item.get("category"),
        item.get("subCategory"),
        item.get("businessSegment"),
        item.get("businessUnit"),
        item.get("businessUnitCustom"),
        item.get("division"),
        item.get("team"),
        item.get("jobFamily"),
    ]
    multi_category = item.get("multi_category")
    if isinstance(multi_category, list):
        for value in multi_category:
            values.append(value.get("category") if isinstance(value, Mapping) else value)
    return _unique_strings(values)


def _description(item: Mapping[str, Any]) -> str:
    parser = item.get("ml_job_parser")
    values = [item.get("description"), item.get("descriptionTeaser")]
    if isinstance(parser, Mapping):
        values.extend(
            [
                parser.get("descriptionTeaser"),
                parser.get("descriptionTeaser_ats"),
                parser.get("descriptionTeaser_first200"),
            ]
        )
    for value in values:
        if _clean_text(value):
            return _html_to_text(value)
    return ""


def _normalize_posting_date(value: Any) -> str:
    if value in (None, "") or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    text = _clean_text(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return ""


def _html_to_text(value: Any) -> str:
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


def _record_page(
    metadata: dict[str, Any],
    response: Any,
    offset: int,
    returned: int,
    normalized: int,
    hits: int,
    config: Mapping[str, Any],
) -> None:
    metadata["pages"].append(
        {
            "offset": offset,
            "url": getattr(response, "url", None),
            "jobs_returned": returned,
            "jobs_normalized": normalized,
            "hits": hits,
            "ref_num": config.get("refNum"),
            "locale": config.get("locale"),
        }
    )
    metadata["offsets"].append(offset)
    metadata["pages_fetched"] = len(metadata["pages"])
