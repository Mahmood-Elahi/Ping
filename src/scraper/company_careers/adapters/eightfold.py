"""Eightfold PCSx public career-search adapter."""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timezone
from html import unescape
from typing import Any, Iterable, List, Mapping, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


PCSX_VARIANT = "pcsx"
SUPPORTED_VARIANTS = {PCSX_VARIANT}
PAGE_SIZE = 10
MAX_PAGES = 5
MAX_RESULTS = PAGE_SIZE * MAX_PAGES
MAX_TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 0.5
PAGE_DELAY_SECONDS = 0.2
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
EXPECTED_ENDPOINT_PATH = "/api/pcsx/search"
JOB_PATH_RE = re.compile(r"^/careers/job/([0-9]+)/*$")
ORGANIZATION_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    re.I,
)
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "job-radar/1.0 (read-only public careers scan)",
}


def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> List[JobRecord]:
    validate_target(target)
    max_pages = max(1, target.max_pages)
    if max_pages > MAX_PAGES:
        raise AdapterError(f"Eightfold max_pages cannot exceed {MAX_PAGES}")
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
    maximum_results = min(MAX_RESULTS, max_pages * PAGE_SIZE)
    metadata: dict[str, Any] = {
        "endpoint_variant": target.endpoint_variant,
        "request_method": "GET",
        "page_size": PAGE_SIZE,
        "maximum_pages": max_pages,
        "maximum_results": maximum_results,
        "total_available": None,
        "initial_total_available": None,
        "final_total_available": None,
        "observed_total_counts": [],
        "total_count_changed": False,
        "jobs_normalized": 0,
        "offsets": [],
        "pages": [],
        "stop_reason": "not_started",
        "diagnostics": [],
        "warnings": [],
    }
    setattr(session, "eightfold_pagination", metadata)

    jobs: list[JobRecord] = []
    seen_job_ids: set[str] = set()
    seen_page_ids: set[tuple[str, ...]] = set()

    for page_index in range(max_pages):
        if len(jobs) >= maximum_results:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        offset = page_index * PAGE_SIZE
        params = {
            "domain": str(target.organization_id),
            "query": "",
            "location": "",
            "start": str(offset),
        }
        response = _get_page(session, target, params, timeout)
        positions, total = _parse_page(
            response,
            offset,
            allow_missing_count=metadata["final_total_available"] is not None,
        )
        _record_total_observation(metadata, offset, total)

        page_ids = tuple(_position_identity(item) for item in positions)
        if positions and page_ids in seen_page_ids:
            metadata["pages"].append(
                {
                    "offset": offset,
                    "url": getattr(response, "url", str(target.endpoint_url)),
                    "jobs_returned": len(positions),
                    "jobs_normalized": 0,
                    "reported_total": total,
                }
            )
            metadata["offsets"].append(offset)
            metadata["pages_fetched"] = len(metadata["pages"])
            metadata["stop_reason"] = "repeated_page"
            break
        seen_page_ids.add(page_ids)

        normalized_count = 0
        new_id_count = 0
        for item in positions:
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
            if len(jobs) >= maximum_results:
                break

        if positions and normalized_count == 0:
            raise AdapterError("Eightfold page contained jobs but none were valid")

        metadata["pages"].append(
            {
                "offset": offset,
                "url": getattr(response, "url", str(target.endpoint_url)),
                "jobs_returned": len(positions),
                "jobs_normalized": new_id_count,
                "reported_total": total,
            }
        )
        metadata["offsets"].append(offset)
        metadata["pages_fetched"] = len(metadata["pages"])
        metadata["jobs_normalized"] = len(jobs)

        if total == 0 and offset == 0 and not jobs:
            metadata["stop_reason"] = "validated_zero_jobs"
            break
        if not positions:
            metadata["stop_reason"] = "empty_page"
            break
        if positions and new_id_count == 0:
            metadata["stop_reason"] = "no_new_stable_ids"
            break
        latest_total = metadata["final_total_available"]
        if latest_total is not None and offset + len(positions) >= latest_total:
            metadata["stop_reason"] = "total_jobs_reached"
            break
        if len(positions) < PAGE_SIZE:
            metadata["stop_reason"] = "short_page"
            break
        if len(jobs) >= maximum_results:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        if page_index + 1 < max_pages:
            time.sleep(PAGE_DELAY_SECONDS)
    else:
        metadata["stop_reason"] = "maximum_pages_reached"

    if metadata["stop_reason"] == "not_started":
        metadata["stop_reason"] = "maximum_pages_reached"
    return jobs


def _record_total_observation(
    metadata: dict[str, Any],
    offset: int,
    total: Optional[int],
) -> None:
    if total is None:
        message = (
            "Eightfold total job count was omitted during pagination; "
            f"using last observed count {metadata['final_total_available']}"
        )
        metadata["warnings"].append(message)
        metadata["diagnostics"].append(
            {"offset": offset, "level": "warning", "warning": message}
        )
        return

    previous_total = metadata["final_total_available"]
    metadata["observed_total_counts"].append(total)
    if metadata["initial_total_available"] is None:
        metadata["initial_total_available"] = total
    elif previous_total is not None and total != previous_total:
        message = (
            "Eightfold total job count changed during pagination: "
            f"{previous_total} -> {total}"
        )
        metadata["total_count_changed"] = True
        metadata["warnings"].append(message)
        metadata["diagnostics"].append(
            {
                "offset": offset,
                "level": "warning",
                "warning": message,
                "previous_total_available": previous_total,
                "total_available": total,
            }
        )
    metadata["final_total_available"] = total
    # Retain the existing field as a compatibility alias for the latest valid hint.
    metadata["total_available"] = total


def validate_target(target: CompanyTarget) -> None:
    missing = [
        name
        for name, value in (
            ("careers_url", target.careers_url),
            ("endpoint_url", target.endpoint_url),
            ("endpoint_variant", target.endpoint_variant),
            ("organization_id", target.organization_id),
        )
        if not str(value or "").strip()
    ]
    if missing:
        raise AdapterError(f"Eightfold target is missing {', '.join(missing)}")
    if target.endpoint_variant not in SUPPORTED_VARIANTS:
        raise AdapterError(
            f"Eightfold endpoint_variant must be one of {sorted(SUPPORTED_VARIANTS)}"
        )
    organization = str(target.organization_id).strip()
    if not ORGANIZATION_RE.fullmatch(organization):
        raise AdapterError("Eightfold organization_id must be an observed domain name")

    careers = urlparse(str(target.careers_url))
    endpoint = urlparse(str(target.endpoint_url))
    if (
        careers.scheme != "https"
        or not careers.hostname
        or careers.query
        or careers.fragment
        or careers.path.rstrip("/") != "/careers"
    ):
        raise AdapterError("Eightfold careers_url must be an observed HTTPS /careers URL")
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.hostname.casefold() != careers.hostname.casefold()
        or endpoint.path != EXPECTED_ENDPOINT_PATH
        or endpoint.query
        or endpoint.fragment
    ):
        raise AdapterError(
            "Eightfold endpoint_url must be the observed same-host /api/pcsx/search endpoint"
        )


def _get_page(
    session: requests.Session,
    target: CompanyTarget,
    params: Mapping[str, str],
    timeout: int,
) -> Any:
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(
                str(target.endpoint_url),
                params=dict(params),
                headers=HEADERS,
                timeout=timeout,
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
            raise AdapterError(f"Eightfold authentication required (HTTP {status})")
        if status in {403, 451}:
            raise AdapterError(f"Eightfold endpoint access was blocked (HTTP {status})")
        if status != 200:
            raise AdapterError(
                f"Eightfold HTTP {status} for {target.name} start={params.get('start')}"
            )
        return response
    raise AdapterError(f"Eightfold request attempts exhausted for {target.name}")


def _parse_page(
    response: Any,
    offset: int,
    *,
    allow_missing_count: bool = False,
) -> tuple[list[Mapping[str, Any]], Optional[int]]:
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    text = str(getattr(response, "text", "") or "")
    if "html" in content_type.casefold() or text.lstrip().startswith("<"):
        lowered = text.casefold()
        if any(token in lowered for token in ("captcha", "access denied", "blocked", "consent")):
            raise AdapterError("Eightfold returned a blocked or consent page")
        if any(token in lowered for token in ("sign in", "log in", "login")):
            raise AdapterError("Eightfold authentication required by successful HTML response")
        if "session" in lowered and any(
            token in lowered for token in ("initialize", "initialise", "required", "expired")
        ):
            raise AdapterError("Eightfold session required by successful HTML response")
        if any(token in lowered for token in ("enable javascript", "javascript required")):
            raise AdapterError("Eightfold browser required by successful HTML response")
        raise AdapterError("Eightfold returned HTML instead of its JSON search response")
    try:
        payload = response.json()
    except (ValueError, requests.RequestException) as exc:
        raise AdapterError(f"Eightfold returned invalid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise AdapterError("Eightfold response must be a JSON object")
    if payload.get("status") != 200:
        raise AdapterError("Eightfold response status must be 200")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise AdapterError("Eightfold response must contain a data object")
    positions = data.get("positions")
    count_present = "count" in data
    total = data.get("count")
    if not isinstance(positions, list):
        raise AdapterError("Eightfold data.positions must be a list")
    if not count_present and allow_missing_count:
        total = None
    elif isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("Eightfold data.count must be a non-negative integer")
    if len(positions) > PAGE_SIZE:
        raise AdapterError("Eightfold page exceeded the observed page size")
    if any(not isinstance(item, Mapping) for item in positions):
        raise AdapterError("Eightfold position entries must be JSON objects")
    if total == 0 and positions:
        raise AdapterError("Eightfold zero-job response unexpectedly contained positions")
    return positions, total


def _normalize_item(item: Mapping[str, Any], target: CompanyTarget) -> JobRecord:
    title = _clean_text(item.get("name"))
    apply_url, position_id = _official_apply_url(target, item.get("positionUrl"))
    external_id = (
        _clean_text(item.get("atsJobId"))
        or _clean_text(item.get("displayJobId"))
        or position_id
    )
    if not title or not external_id:
        raise AdapterError("Eightfold position is missing title or a stable identifier")

    locations = _location_values(item)
    work_mode = _work_mode(item)
    location_display = list(locations)
    if work_mode and not any(work_mode.casefold() in value.casefold() for value in location_display):
        location_display.append(work_mode)
    department = _clean_text(item.get("department"))
    description = _description(item)
    host = (urlparse(str(target.endpoint_url)).hostname or "").casefold()
    return JobRecord(
        job_id=f"eightfold:{host}:{external_id}",
        title=title,
        company=target.name,
        location=" | ".join(location_display),
        url=apply_url,
        updated_at=_normalize_posting_date(item.get("postedTs")),
        source=f"eightfold:{host}",
        departments=[department] if department else [],
        offices=locations,
        description=description or None,
        raw_metadata={
            "platform": "eightfold",
            "endpoint_variant": target.endpoint_variant,
            "organization_id": target.organization_id,
            "external_job_id": external_id,
            "position_id": position_id,
            "position": dict(item),
        },
    )


def _official_apply_url(target: CompanyTarget, value: Any) -> tuple[str, str]:
    careers_url = str(target.careers_url)
    url = urljoin(careers_url.rstrip("/") + "/", str(value or "").strip())
    careers = urlparse(careers_url)
    parsed = urlparse(url)
    match = JOB_PATH_RE.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.casefold() != (careers.hostname or "").casefold()
        or not match
        or parsed.query
    ):
        raise AdapterError("Eightfold position has no official stable apply URL")
    return urlunparse(parsed._replace(fragment="")), match.group(1)


def _position_identity(item: Mapping[str, Any]) -> str:
    return (
        _clean_text(item.get("atsJobId"))
        or _clean_text(item.get("displayJobId"))
        or _clean_text(item.get("id"))
        or _clean_text(item.get("positionUrl"))
    )


def _location_values(item: Mapping[str, Any]) -> list[str]:
    raw_locations = item.get("locations")
    if not isinstance(raw_locations, list) or not raw_locations:
        raw_locations = item.get("standardizedLocations")
    if raw_locations is None:
        return []
    if not isinstance(raw_locations, list):
        raw_locations = [raw_locations]
    return _unique_strings(raw_locations)


def _work_mode(item: Mapping[str, Any]) -> str:
    values = [item.get("workLocationOption"), item.get("locationFlexibility")]
    cleaned = _unique_strings(values)
    important = [
        value.replace("_", " ").title()
        for value in cleaned
        if "remote" in value.casefold() or "hybrid" in value.casefold()
    ]
    return " / ".join(important)


def _description(item: Mapping[str, Any]) -> str:
    for field in ("jobDescription", "description", "job_description"):
        if item.get(field):
            return _html_to_text(item[field])
    return ""


def _normalize_posting_date(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, bool):
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
