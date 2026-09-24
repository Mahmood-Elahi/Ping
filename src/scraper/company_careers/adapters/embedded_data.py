"""Bounded collector for reviewed JSON embedded in anonymous page source."""

from __future__ import annotations

import hashlib
import json
import re
import string
import time
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from lxml import html

from src.scraper.company_careers.adapters import static_html
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    JobRecord,
    SourceScanMetadata,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.listing_completion import (
    AdaptiveListingCompletion,
    record_terminal_evidence,
)


MAX_PAGES = 2
MAX_RESULTS = 20
MAX_ATTEMPTS = 2
MAX_TIMEOUT_SECONDS = 60
MAX_RESPONSE_BYTES = 3_000_000
MAX_SCRIPT_BYTES = 1_000_000
MAX_SCRIPTS = 20
RETRY_DELAY_SECONDS = 0.1
TRANSIENT_STATUSES = {202, 408, 425, 500, 502, 503, 504}
HEADERS = {"Accept": "text/html,application/xhtml+xml", "User-Agent": "job-radar/1.0"}
SUPPORTED_VARIANTS = {
    "json_ld_job_posting",
    "inline_json",
    "next_data",
    "html_attribute_json",
    "public_json_reference",
    "public_json_endpoint",
    "next_flight_collection",
}
_PATH_RE = re.compile(r"^[A-Za-z0-9_$-]+(?:\.[A-Za-z0-9_$-]+|\[[0-9]+\])*$")
_FIELD_RE = re.compile(r"^[A-Za-z0-9_@-]+(?:\.[A-Za-z0-9_@-]+|\[[0-9]+\])*$")


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 60) -> list[JobRecord]:
    validate_target(target)
    maximum_pages = min(int(target.max_pages), MAX_PAGES)
    maximum_jobs = min(int(target.max_jobs), MAX_RESULTS)
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
    metadata = SourceScanMetadata(**{
        "endpoint_variant": target.endpoint_variant, "request_method": "GET", "pages": [],
        "pages_fetched": 0, "scripts_inspected": 0, "bytes_inspected": 0,
        "jobs_normalized": 0, "maximum_pages": maximum_pages, "maximum_results": maximum_jobs,
        "stop_reason": "not_started", "repeated_page": False, "zero_job_contract": False,
        "diagnostics": [], "warnings": [],
    })
    setattr(session, "embedded_data_pagination", metadata)
    publish_source_scan_metadata(session, metadata)
    listing_budget = AdaptiveListingCompletion(maximum_pages, maximum_jobs)
    listing_budget.record(metadata)
    explicit_pagination = bool(target.pagination_url_template)
    listing_budget.record_terminal_probe_decision(
        metadata,
        max(1, int(target.page_size)),
        requests_completed=0,
        results_observed=0,
        deterministic_terminal_contract=explicit_pagination,
    )
    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    seen_pages: set[tuple[str, ...]] = set()

    for page_index in range(int(metadata["effective_listing_request_ceiling"])):
        url = (str(target.pagination_url_template).format(page=page_index + 1)
               if target.pagination_url_template else str(target.listing_url))
        metadata["pages_requested"] += 1
        response = _get(session, url, timeout)
        if target.endpoint_variant == "public_json_endpoint":
            items, scripts, inspected_bytes, valid_empty = _extract_direct_json(
                response, target
            )
        elif target.endpoint_variant == "next_flight_collection":
            items, scripts, inspected_bytes, valid_empty = _extract_next_flight(
                response, target
            )
        elif target.endpoint_variant == "html_attribute_json":
            items, scripts, inspected_bytes, valid_empty = _extract_attribute_json(
                response, target
            )
        elif target.endpoint_variant == "public_json_reference":
            items, scripts, inspected_bytes, valid_empty = _extract_public_json(
                session, response, target, timeout
            )
        else:
            items, scripts, inspected_bytes, valid_empty = _extract(response, target)
        effective_jobs = (
            listing_budget.complete_collection_job_ceiling(len(items))
            if not explicit_pagination
            else int(metadata["effective_listing_job_ceiling"])
        )
        metadata["scripts_inspected"] += scripts
        metadata["bytes_inspected"] += inspected_bytes
        metadata["pages_completed"] += 1
        identities = tuple(_item_identity(item, target) for item in items)
        record = {"page": page_index + 1, "url": url, "jobs_returned": len(items), "jobs_normalized": 0}
        metadata["pages"].append(record)
        metadata["pages_fetched"] = len(metadata["pages"])
        if items and identities in seen_pages:
            metadata["repeated_page"] = True
            metadata["stop_reason"] = "repeated_data"
            break
        seen_pages.add(identities)
        new_ids = 0
        parsed = 0
        for item in items:
            try:
                job = _normalize(item, target, url)
            except AdapterError as exc:
                metadata["diagnostics"].append({"page": page_index + 1, "error": str(exc)})
                continue
            parsed += 1
            if job.job_id in seen_ids:
                metadata["stop_reason"] = "repeated_job_id"
                metadata["scan_completeness_reason"] = "pagination_failure"
                raise AdapterError(f"Embedded-data repeated stable job ID: {job.job_id}")
            seen_ids.add(job.job_id)
            jobs.append(job)
            new_ids += 1
            if len(jobs) >= effective_jobs:
                break
        if items and parsed == 0:
            if metadata["diagnostics"]:
                raise AdapterError(str(metadata["diagnostics"][0]["error"]))
            raise AdapterError("Embedded-data collection contained jobs but none satisfied the reviewed schema")
        record["jobs_normalized"] = new_ids
        metadata["jobs_normalized"] = len(jobs)
        if page_index == 0 and not items:
            if not valid_empty:
                raise AdapterError("Embedded-data response did not prove a valid empty collection")
            metadata["zero_job_contract"] = True
            metadata["stop_reason"] = "validated_zero_jobs"
            metadata["scan_complete"] = True
            metadata["scan_completeness_reason"] = "validated_zero_jobs"
            metadata["total_available"] = 0
            record_terminal_evidence(
                metadata,
                "terminal_page",
                "validated_zero_jobs",
                has_more=False,
                terminal_page_observed=True,
            )
            break
        if not items and target.pagination_url_template and valid_empty:
            metadata["stop_reason"] = "validated_terminal_empty_page"
            metadata["scan_complete"] = True
            metadata["scan_completeness_reason"] = "validated_terminal_empty_page"
            metadata["total_available"] = len(jobs)
            record_terminal_evidence(
                metadata,
                "terminal_page",
                "validated_terminal_empty_page",
                has_more=False,
                terminal_page_observed=True,
            )
            break
        if new_ids == 0:
            metadata["stop_reason"] = "no_new_stable_ids"
            metadata["scan_completeness_reason"] = "pagination_failure"
            break
        if not target.pagination_url_template:
            metadata["stop_reason"] = "complete_structured_collection"
            metadata["total_available"] = len(items)
            record_terminal_evidence(
                metadata,
                "complete_collection_response",
                len(items),
                has_more=False,
                terminal_page_observed=True,
                complete_collection_response=True,
            )
            if len(items) > effective_jobs:
                metadata["scan_completeness_reason"] = "safety_limit_reached"
                metadata["safety_limit_reached"] = True
                metadata["stop_reason"] = "maximum_results_reached"
            else:
                metadata["scan_complete"] = True
                metadata["scan_completeness_reason"] = "complete_structured_collection"
            break
        if len(jobs) >= int(metadata["effective_listing_job_ceiling"]):
            metadata["stop_reason"] = "maximum_results_reached"
            metadata["scan_completeness_reason"] = "safety_limit_reached"
            metadata["safety_limit_reached"] = True
            break
        if page_index + 1 < maximum_pages:
            time.sleep(RETRY_DELAY_SECONDS)
    else:
        metadata["stop_reason"] = "maximum_pages_reached"
        metadata["scan_completeness_reason"] = "safety_limit_reached"
        metadata["safety_limit_reached"] = True
    return jobs


def validate_target(target: CompanyTarget) -> None:
    missing = [name for name, value in (
        ("listing_url", target.listing_url), ("endpoint_variant", target.endpoint_variant),
        ("allowed_apply_hosts", target.allowed_apply_hosts),
    ) if not value]
    if missing:
        raise AdapterError(f"Embedded-data target is missing {', '.join(missing)}")
    if target.endpoint_variant not in SUPPORTED_VARIANTS:
        raise AdapterError(f"Embedded-data unsupported variant: {target.endpoint_variant}")
    _require_https(str(target.listing_url), "listing_url")
    if target.pagination_url_template:
        if str(target.pagination_url_template).count("{page}") != 1:
            raise AdapterError("Embedded-data pagination_url_template must contain one {page} placeholder")
        _require_https(str(target.pagination_url_template).replace("{page}", "1"), "pagination_url_template")
    if target.endpoint_variant not in {"json_ld_job_posting", "public_json_endpoint"}:
        if not target.embedded_script_selector or not target.embedded_json_path:
            raise AdapterError("Embedded-data target requires a reviewed script selector and JSON path")
        static_html.selector_to_xpath(str(target.embedded_script_selector))
        _validate_path(str(target.embedded_json_path), "JSON path")
    if target.endpoint_variant == "next_flight_collection" and not re.fullmatch(
        r"[A-Za-z0-9_$-]+", str(target.embedded_json_path)
    ):
        raise AdapterError(
            "Embedded-data Next.js Flight collection path must be one top-level field"
        )
    if target.endpoint_variant == "html_attribute_json":
        if not target.reference_url_attribute:
            raise AdapterError(
                "Embedded-data HTML attribute JSON requires reference_url_attribute"
            )
        if not re.fullmatch(
            r"[A-Za-z_:][A-Za-z0-9_.:-]*", str(target.reference_url_attribute)
        ):
            raise AdapterError("Embedded-data JSON attribute name is invalid")
        if not target.allowed_source_hosts:
            raise AdapterError(
                "Embedded-data HTML attribute JSON requires allowed_source_hosts"
            )
        if not target.job_attribution_field or not target.job_attribution_regex:
            raise AdapterError(
                "Embedded-data HTML attribute JSON requires per-job attribution"
            )
    if target.endpoint_variant == "public_json_endpoint":
        if not target.embedded_json_path:
            raise AdapterError("Embedded-data public JSON endpoint requires embedded_json_path")
        _validate_path(str(target.embedded_json_path), "JSON path")
        if not target.allowed_source_hosts:
            raise AdapterError("Embedded-data public JSON endpoint requires allowed_source_hosts")
        if not target.job_attribution_field or not target.job_attribution_regex:
            raise AdapterError(
                "Embedded-data public JSON endpoint requires a per-job attribution field and regex"
            )
    if target.endpoint_variant == "public_json_reference":
        if not target.endpoint_url or not target.reference_url_attribute:
            raise AdapterError("Embedded-data public JSON reference requires endpoint_url and reference_url_attribute")
        _require_https(str(target.endpoint_url), "endpoint_url")
        if not re.fullmatch(r"[A-Za-z_:][A-Za-z0-9_.:-]*", str(target.reference_url_attribute)):
            raise AdapterError("Embedded-data reference_url_attribute is invalid")
    for field_name, value in (("id_field", target.id_field), ("title_field", target.title_field),
                              ("location_field", target.location_field), ("url_field", target.url_field),
                              ("date_field", target.date_field), ("department_field", target.department_field),
                              ("remote_field", target.remote_field),
                              ("job_attribution_field", target.job_attribution_field)):
        if value and not _FIELD_RE.fullmatch(str(value)):
            raise AdapterError(f"Embedded-data {field_name} is invalid")
    for value in target.location_fields:
        if not _FIELD_RE.fullmatch(str(value)):
            raise AdapterError("Embedded-data location_fields contains an invalid field")
    if target.endpoint_variant != "json_ld_job_posting" and not target.title_field:
        raise AdapterError("Embedded-data target requires title_field")
    if target.endpoint_variant != "json_ld_job_posting" and not target.id_field and not target.external_id_regex and not target.allow_url_id_fallback:
        raise AdapterError("Embedded-data target requires a stable identifier contract")
    if target.endpoint_variant != "json_ld_job_posting" and not target.url_field:
        raise AdapterError("Embedded-data target requires url_field")
    if target.external_id_regex:
        static_html._safe_regex(str(target.external_id_regex))
    if target.job_attribution_regex:
        static_html._safe_regex(str(target.job_attribution_regex))
    if target.job_attribution_exclude_regex:
        static_html._safe_regex(str(target.job_attribution_exclude_regex))
    if target.apply_url_template:
        try:
            parsed_template = list(string.Formatter().parse(str(target.apply_url_template)))
        except ValueError as exc:
            raise AdapterError(f"Embedded-data apply URL template is invalid: {exc}") from exc
        for _, field_name, format_spec, conversion in parsed_template:
            if field_name and (not _FIELD_RE.fullmatch(field_name) or format_spec or conversion):
                raise AdapterError("Embedded-data apply URL template uses an unsafe field")
        _require_https(re.sub(r"\{[^{}]+\}", "value", str(target.apply_url_template)), "apply_url_template")
    static_html._validate_hosts(target.allowed_apply_hosts)
    static_html._validate_hosts(target.allowed_source_hosts)
    if not 1 <= int(target.max_pages) <= MAX_PAGES or not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError("Embedded-data page or result limit exceeds the hard maximum")
    if not 1 <= int(target.max_scripts) <= MAX_SCRIPTS:
        raise AdapterError("Embedded-data script-count limit exceeds the hard maximum")
    if not 1 <= int(target.max_response_bytes) <= MAX_RESPONSE_BYTES:
        raise AdapterError("Embedded-data response byte limit exceeds the hard maximum")
    if not 1 <= int(target.max_script_bytes) <= MAX_SCRIPT_BYTES:
        raise AdapterError("Embedded-data script byte limit exceeds the hard maximum")


def _extract(response: Any, target: CompanyTarget) -> tuple[list[Mapping[str, Any]], int, int, bool]:
    content = getattr(response, "content", None)
    if content is None:
        content = str(getattr(response, "text", "") or "").encode("utf-8")
    if not content:
        raise AdapterError("Embedded-data returned an empty HTTP 200 response")
    if len(content) > int(target.max_response_bytes):
        raise AdapterError("Embedded-data response exceeded byte limit")
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and "html" not in content_type.casefold():
        raise AdapterError("Embedded-data page returned a non-HTML response")
    try:
        root = html.fromstring(content)
    except (ValueError, TypeError) as exc:
        raise AdapterError(f"Embedded-data malformed HTTP 200 response: {exc}") from exc
    _reject_page(root)
    selector = ("script[type='application/ld+json']" if target.endpoint_variant == "json_ld_job_posting"
                else str(target.embedded_script_selector))
    scripts = static_html.select(root, selector)
    if len(scripts) > int(target.max_scripts):
        raise AdapterError("Embedded-data script-count limit exceeded")
    if not scripts:
        raise AdapterError("Embedded-data reviewed script selector did not match")
    results: list[Mapping[str, Any]] = []
    inspected = len(content)
    matched_schema = False
    for script in scripts:
        if script.get("src"):
            raise AdapterError("Embedded-data generic external script bundles are unsupported")
        body = script.text or ""
        byte_count = len(body.encode("utf-8"))
        if byte_count > int(target.max_script_bytes):
            raise AdapterError("Embedded-data script byte limit exceeded")
        inspected += byte_count
        try:
            payload = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"Embedded-data malformed JSON: {exc}") from exc
        if target.endpoint_variant == "json_ld_job_posting":
            found = _json_ld_jobs(payload)
        else:
            found_value = json_path(payload, str(target.embedded_json_path))
            if not isinstance(found_value, list):
                raise AdapterError("Embedded-data reviewed JSON path is not a list")
            found = found_value
        if not all(isinstance(item, Mapping) for item in found):
            raise AdapterError("Embedded-data reviewed collection contains unsupported values")
        matched_schema = True
        results.extend(found)
    return results, len(scripts), inspected, matched_schema and not results


def _extract_next_flight(
    response: Any, target: CompanyTarget
) -> tuple[list[Mapping[str, Any]], int, int, bool]:
    """Read one reviewed list from bounded Next.js Flight inline chunks.

    Flight pages serialize server-rendered data into inert
    ``self.__next_f.push([1, "..."])`` calls. This variant decodes only that
    exact JSON argument shape and then reads one explicitly configured
    top-level collection. It never evaluates JavaScript or follows bundles.
    """
    content = _bounded_content(response, target, expected="html")
    try:
        root = html.fromstring(content)
    except (ValueError, TypeError) as exc:
        raise AdapterError(f"Embedded-data malformed HTTP 200 response: {exc}") from exc
    _reject_page(root)

    selected = static_html.select(root, str(target.embedded_script_selector))
    prefix = "self.__next_f.push("
    scripts = [node for node in selected if (node.text or "").strip().startswith(prefix)]
    if len(scripts) > int(target.max_scripts):
        raise AdapterError("Embedded-data script-count limit exceeded")
    if not scripts:
        raise AdapterError("Embedded-data reviewed Flight script selector did not match")

    collection_name = str(target.embedded_json_path)
    marker = json.dumps(collection_name) + ":"
    collections: list[list[Any]] = []
    inspected = len(content)
    for script in scripts:
        if script.get("src"):
            raise AdapterError("Embedded-data generic external script bundles are unsupported")
        body = (script.text or "").strip()
        byte_count = len(body.encode("utf-8"))
        if byte_count > int(target.max_script_bytes):
            raise AdapterError("Embedded-data script byte limit exceeded")
        inspected += byte_count
        if not body.endswith(")"):
            raise AdapterError("Embedded-data malformed Next.js Flight wrapper")
        try:
            wrapper = json.loads(body[len(prefix):-1])
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"Embedded-data malformed Next.js Flight JSON: {exc}") from exc
        if (
            not isinstance(wrapper, list)
            or len(wrapper) != 2
            or wrapper[0] != 1
            or not isinstance(wrapper[1], str)
        ):
            raise AdapterError("Embedded-data unsupported Next.js Flight wrapper")
        stream = wrapper[1]
        start = 0
        while True:
            index = stream.find(marker, start)
            if index < 0:
                break
            try:
                value, consumed = json.JSONDecoder().raw_decode(
                    stream[index + len(marker):]
                )
            except (TypeError, ValueError) as exc:
                raise AdapterError(
                    f"Embedded-data malformed Next.js Flight collection: {exc}"
                ) from exc
            if not isinstance(value, list):
                raise AdapterError(
                    "Embedded-data reviewed Next.js Flight collection is not a list"
                )
            collections.append(value)
            start = index + len(marker) + consumed

    if len(collections) != 1:
        raise AdapterError(
            "Embedded-data reviewed Next.js Flight collection must occur exactly once"
        )
    found = collections[0]
    if not all(isinstance(item, Mapping) for item in found):
        raise AdapterError(
            "Embedded-data reviewed Next.js Flight collection contains unsupported values"
        )
    return list(found), len(scripts), inspected, not found


def _extract_public_json(
    session: requests.Session, response: Any, target: CompanyTarget, timeout: int
) -> tuple[list[Mapping[str, Any]], int, int, bool]:
    content = _bounded_content(response, target, expected="html")
    try:
        root = html.fromstring(content)
    except (ValueError, TypeError) as exc:
        raise AdapterError(f"Embedded-data malformed HTTP 200 response: {exc}") from exc
    _reject_page(root)
    references = static_html.select(root, str(target.embedded_script_selector))
    if len(references) > int(target.max_scripts):
        raise AdapterError("Embedded-data script-count limit exceeded")
    if len(references) != 1:
        raise AdapterError("Embedded-data public JSON reference selector must match exactly once")
    observed = urljoin(str(target.listing_url), str(references[0].get(str(target.reference_url_attribute)) or ""))
    configured = urlunparse(urlparse(str(target.endpoint_url))._replace(fragment=""))
    if urlunparse(urlparse(observed)._replace(fragment="")) != configured:
        raise AdapterError("Embedded-data public JSON endpoint was not explicitly referenced by the page")
    data_response = _get(session, configured, timeout)
    data_content = _bounded_content(data_response, target, expected="json")
    try:
        payload = json.loads(data_content)
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"Embedded-data malformed JSON: {exc}") from exc
    found = json_path(payload, str(target.embedded_json_path))
    if not isinstance(found, list) or not all(isinstance(item, Mapping) for item in found):
        raise AdapterError("Embedded-data reviewed JSON path is not a job-object list")
    return list(found), 1, len(content) + len(data_content), not found


def _extract_attribute_json(
    response: Any, target: CompanyTarget
) -> tuple[list[Mapping[str, Any]], int, int, bool]:
    """Read a reviewed JSON bootstrap from one explicit HTML attribute.

    Some public ATS pages server-render their initial job collection in a hidden
    input instead of a script tag.  This variant deliberately supports only one
    configured element and one configured attribute; it does not execute client
    code or inspect arbitrary bundles.
    """
    _validate_source_response(response, target)
    content = _bounded_content(response, target, expected="html")
    try:
        root = html.fromstring(content)
    except (ValueError, TypeError) as exc:
        raise AdapterError(f"Embedded-data malformed HTTP 200 response: {exc}") from exc
    _reject_page(root)
    nodes = static_html.select(root, str(target.embedded_script_selector))
    if len(nodes) != 1:
        raise AdapterError(
            "Embedded-data HTML attribute selector must match exactly once"
        )
    attribute = str(target.reference_url_attribute)
    body = nodes[0].get(attribute)
    if body is None or not str(body).strip():
        raise AdapterError("Embedded-data reviewed JSON attribute is empty")
    body_bytes = str(body).encode("utf-8")
    if len(body_bytes) > int(target.max_script_bytes):
        raise AdapterError("Embedded-data JSON attribute exceeded byte limit")
    try:
        payload = json.loads(str(body))
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"Embedded-data malformed attribute JSON: {exc}") from exc
    found = json_path(payload, str(target.embedded_json_path))
    if not isinstance(found, list) or not all(isinstance(item, Mapping) for item in found):
        raise AdapterError(
            "Embedded-data reviewed JSON attribute path is not a job-object list"
        )
    _require_exact_attribution(found, target)
    return list(found), 1, len(content) + len(body_bytes), not found


def _extract_direct_json(
    response: Any, target: CompanyTarget
) -> tuple[list[Mapping[str, Any]], int, int, bool]:
    _validate_source_response(response, target)
    content = _bounded_content(response, target, expected="json")
    try:
        payload = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"Embedded-data malformed JSON: {exc}") from exc
    found = json_path(payload, str(target.embedded_json_path))
    if not isinstance(found, list) or not all(isinstance(item, Mapping) for item in found):
        raise AdapterError("Embedded-data reviewed JSON path is not a job-object list")
    _require_exact_attribution(found, target)
    return list(found), 0, len(content), not found


def _require_exact_attribution(
    items: Iterable[Mapping[str, Any]], target: CompanyTarget
) -> None:
    attribution = static_html._safe_regex(str(target.job_attribution_regex))
    exclusion = (
        static_html._safe_regex(str(target.job_attribution_exclude_regex))
        if target.job_attribution_exclude_regex
        else None
    )
    for item in items:
        value = _string(_field(item, target.job_attribution_field))
        if not value or not attribution.search(value) or (exclusion and exclusion.search(value)):
            raise AdapterError("Embedded-data job failed exact source attribution")


def _validate_source_response(response: Any, target: CompanyTarget) -> None:
    final_url = str(getattr(response, "url", "") or target.listing_url or "")
    parsed = urlparse(final_url)
    host = (parsed.hostname or "").casefold()
    allowed = [str(value).casefold() for value in target.allowed_source_hosts]
    if parsed.scheme != "https" or not any(
        host == value or host.endswith("." + value) for value in allowed
    ):
        raise AdapterError("Embedded-data JSON endpoint redirected outside allowed source hosts")


def _bounded_content(response: Any, target: CompanyTarget, *, expected: str) -> bytes:
    content = getattr(response, "content", None)
    if content is None:
        content = str(getattr(response, "text", "") or "").encode("utf-8")
    if not content:
        raise AdapterError("Embedded-data returned an empty HTTP 200 response")
    if len(content) > int(target.max_response_bytes):
        raise AdapterError("Embedded-data response exceeded byte limit")
    content_type = str(getattr(response, "headers", {}).get("content-type", "")).casefold()
    if content_type and expected not in content_type:
        raise AdapterError(f"Embedded-data endpoint returned a non-{expected.upper()} response")
    return bytes(content)


def json_path(value: Any, path: str) -> Any:
    _validate_path(path, "JSON path")
    current = value
    for name, index in re.findall(r"(?:^|\.)([A-Za-z0-9_$-]+)|\[([0-9]+)\]", path):
        if name:
            if not isinstance(current, Mapping) or name not in current:
                raise AdapterError(f"Embedded-data reviewed JSON path is missing component: {name}")
            current = current[name]
        else:
            if not isinstance(current, list) or int(index) >= len(current):
                raise AdapterError("Embedded-data reviewed JSON path index is missing")
            current = current[int(index)]
    return current


def _validate_path(path: str, label: str) -> None:
    if len(path) > 300 or not _PATH_RE.fullmatch(path):
        raise AdapterError(f"Embedded-data {label} is invalid")


def _json_ld_jobs(payload: Any) -> list[Mapping[str, Any]]:
    values = payload if isinstance(payload, list) else payload.get("@graph", [payload]) if isinstance(payload, Mapping) else []
    return [item for item in values if isinstance(item, Mapping) and str(item.get("@type", "")).casefold() == "jobposting"]


def _normalize(item: Mapping[str, Any], target: CompanyTarget, page_url: str) -> JobRecord:
    if target.endpoint_variant == "json_ld_job_posting":
        title = _string(item.get("title"))
        raw_url = item.get("url")
        raw_id = item.get("identifier")
        external_id = _string(raw_id.get("value") if isinstance(raw_id, Mapping) else raw_id)
        locations = _json_ld_locations(item.get("jobLocation"))
        departments = _values(item.get("occupationalCategory"))
        updated = _string(item.get("datePosted"))
        remote = _string(item.get("jobLocationType"))
    else:
        title = _string(_normalization_field(item, target, target.title_field))
        raw_url = _normalization_field(item, target, target.url_field)
        external_id = (
            _string(_normalization_field(item, target, target.id_field))
            if target.id_field else ""
        )
        if target.location_fields:
            locations = _unique(
                value
                for field_name in target.location_fields
                for value in _values(_field(item, field_name))
            )
        else:
            locations = _values(_field(item, target.location_field)) if target.location_field else []
        departments = _values(_field(item, target.department_field)) if target.department_field else []
        updated = _string(_field(item, target.date_field)) if target.date_field else ""
        remote = _string(_field(item, target.remote_field)) if target.remote_field else ""
    raw_url_text = _string(raw_url)
    if target.apply_url_template:
        fields = {str(key): _string(value) for key, value in item.items() if isinstance(value, (str, int, float))}
        fields.update({"value": raw_url_text, "id": external_id})
        try:
            raw_url_text = str(target.apply_url_template).format_map(fields)
        except KeyError as exc:
            raise AdapterError(f"Embedded-data apply URL template field is missing: {exc}") from exc
    url = static_html._official_url(urljoin(page_url, raw_url_text), target)
    if not external_id and target.external_id_regex:
        match = static_html._safe_regex(str(target.external_id_regex)).search(url)
        external_id = match.group(1) if match and match.groups() else match.group(0) if match else ""
    if not external_id and target.allow_url_id_fallback:
        canonical = urlunparse(urlparse(url)._replace(query="", fragment=""))
        external_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    if not title or not external_id:
        raise AdapterError("Embedded-data job is missing title or stable identifier")
    work_modes = [value for value in locations + [remote] if re.search(r"(?i)\b(remote|hybrid|telecommute)\b", value)]
    locations = _unique(locations + ([remote] if remote else []))
    host = (urlparse(str(target.listing_url)).hostname or "").casefold()
    return JobRecord(
        job_id=f"embedded_data:{host}:{external_id}", title=title, company=target.name,
        location=" | ".join(locations), url=url, updated_at=updated,
        source=f"embedded_data:{host}", departments=departments, offices=locations,
        raw_metadata={"platform": "javascript_embedded_data", "endpoint_variant": target.endpoint_variant,
                      "external_job_id": external_id, "work_modes": work_modes},
    )


def _field(item: Mapping[str, Any], path: Optional[str]) -> Any:
    return json_path(item, str(path)) if path else None


_NEXT_DATA_PRISMIC_ALIASES = {
    "label": ("data.title",),
    "link.url": ("url",),
    "link.id": ("id",),
}


def _normalization_field(
    item: Mapping[str, Any], target: CompanyTarget, path: Optional[str]
) -> Any:
    """Read reviewed Next.js/Prismic document fields across its two schemas."""
    paths = [str(path)] if path else []
    if target.endpoint_variant == "next_data":
        paths.extend(_NEXT_DATA_PRISMIC_ALIASES.get(str(path), ()))
    last_error: Optional[AdapterError] = None
    for candidate in paths:
        try:
            return _field(item, candidate)
        except AdapterError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    return None


def _json_ld_locations(value: Any) -> list[str]:
    locations = value if isinstance(value, list) else [value]
    result = []
    for location in locations:
        if isinstance(location, Mapping):
            address = location.get("address", location)
            if isinstance(address, Mapping):
                result.append(", ".join(_string(address.get(key)) for key in ("addressLocality", "addressRegion", "addressCountry") if _string(address.get(key))))
            else:
                result.extend(_values(address))
        else:
            result.extend(_values(location))
    return _unique(result)


def _values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return _unique(_string(item) for item in value)
    if isinstance(value, Mapping):
        for key in ("name", "value", "label"):
            if key in value:
                return _values(value[key])
        return []
    return [_string(value)] if _string(value) else []


def _item_identity(item: Mapping[str, Any], target: CompanyTarget) -> str:
    if target.url_field:
        try:
            return _string(_field(item, target.url_field))
        except AdapterError:
            pass
    if target.id_field:
        try:
            return _string(_field(item, target.id_field))
        except AdapterError:
            pass
    return json.dumps(item, sort_keys=True, default=str)[:300]


def _get(session: requests.Session, url: str, timeout: int) -> Any:
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(url, headers=HEADERS, timeout=timeout)
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
            raise AdapterError(f"Embedded-data authentication required (HTTP {status})")
        if status in {403, 429, 451}:
            raise AdapterError(f"Embedded-data blocked response (HTTP {status})")
        if status != 200:
            raise AdapterError(f"Embedded-data HTTP {status}")
        return response
    raise AdapterError("Embedded-data request attempts exhausted")


def _reject_page(root: Any) -> None:
    text = " ".join(root.text_content().split()).casefold()
    title = " ".join(root.xpath("string(//title)").split()).casefold()
    if any(token in text for token in ("cloudflare challenge", "access denied", "request blocked", "captcha")):
        raise AdapterError("Embedded-data blocked response")
    if "consent required" in text or "cookie consent required" in text:
        raise AdapterError("Embedded-data consent page is unsupported")
    if "enable javascript to continue" in text or "javascript is required" in text:
        raise AdapterError("Embedded-data browser required")
    if re.search(r"\b(session required|session expired)\b", title):
        raise AdapterError("Embedded-data session required")
    if re.search(r"\b(sign in|log in|login)\b", title):
        raise AdapterError("Embedded-data authentication required")


def _require_https(url: str, field: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise AdapterError(f"Embedded-data {field} must be an anonymous HTTPS URL")
    if any(token in parsed.path.casefold() for token in ("/login", "/signin", "/session")):
        raise AdapterError(f"Embedded-data {field} cannot require authentication or session state")


def _string(value: Any) -> str:
    return " ".join(str(value or "").split())


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _string(value).strip(" |")
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            result.append(text)
    return result
