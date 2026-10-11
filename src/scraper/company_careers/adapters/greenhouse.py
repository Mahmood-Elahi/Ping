"""Bounded Greenhouse job board adapter."""

from __future__ import annotations

import json
import re
from typing import Any, List, Mapping
from urllib.parse import parse_qs, parse_qsl, urlparse, urlunparse

import requests

from src.scraper.company_careers.incremental import (
    IncrementalCapability,
    IncrementalPolicy,
)
from src.scraper.company_careers.models import (
    ADAPTER_SAFETY_BUDGET_DEFAULTS,
    AdapterError,
    AdapterResult,
    CompanyTarget,
    InventoryItem,
    ItemFailure,
    JobRecord,
    SourceScanMetadata,
    explicit_adapter_result,
    publish_source_scan_metadata,
)
from src.scraper.company_careers.student_roles import (
    classify_student_location,
    student_job_signals,
)


MAX_RESULTS = ADAPTER_SAFETY_BUDGET_DEFAULTS["greenhouse"]["max_jobs"]
MAX_DETAIL_REQUESTS = 20
MAX_RESPONSE_BYTES = 5_000_000
MAX_DETAIL_RESPONSE_BYTES = 1_000_000
READ_CHUNK_BYTES = 64 * 1024
HEADERS = {"Accept": "application/json", "User-Agent": "job-radar/1.0"}
STRICT_BOARD_VARIANT = "boards_api_v1_strict"
STABLE_ID_TEMPLATE_FIELD = "{id}"
QUERY_PARAMETER_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")

# Provider-contract review: the public board endpoint returns one complete
# active-board collection and exposes a listing ``updated_at`` value, but the
# repository contains no provider contract proving that value is a material
# job version.  There is also no reviewed update filter, cursor, removal feed,
# or conditional representation validator.  A seen ID or matching timestamp
# therefore cannot skip hydration, and no Greenhouse checkpoint may advance.
INCREMENTAL_POLICY = IncrementalPolicy(
    capability=IncrementalCapability.NONE,
    strategy_version="greenhouse_v1",
)
TRUSTED_LISTING_VERSION_FIELD: None = None


def _secure_apply_url(value: object) -> str:
    """Canonicalize legacy Greenhouse HTTP links to their HTTPS endpoint."""
    parsed = urlparse(str(value or ""))
    if parsed.scheme.casefold() == "http" and parsed.hostname:
        return urlunparse(parsed._replace(scheme="https"))
    return str(value or "")


@explicit_adapter_result
def fetch_jobs(
    session: requests.Session, target: CompanyTarget, timeout: int = 20
) -> AdapterResult:
    """Fetch a compact board index and enrich at most 20 postings."""
    metadata = SourceScanMetadata(stop_reason="not_started")
    publish_source_scan_metadata(session, metadata)
    try:
        return _fetch_jobs(session, target, timeout, metadata)
    except requests.RequestException:
        metadata["stop_reason"] = "request_failure"
        metadata["scan_completeness_reason"] = "adapter_failure"
        raise
    except AdapterError:
        if metadata["stop_reason"] == "not_started":
            metadata["stop_reason"] = "adapter_failure"
        metadata["scan_completeness_reason"] = "adapter_failure"
        raise


def _fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int,
    metadata: SourceScanMetadata,
) -> AdapterResult:
    if not target.slug:
        raise AdapterError("Greenhouse target is missing slug")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"Greenhouse max_jobs cannot exceed {MAX_RESULTS}")
    if not 1 <= int(target.max_response_bytes) <= MAX_RESPONSE_BYTES:
        raise AdapterError("Greenhouse response byte limit exceeds the hard maximum")
    apply_url_contract = _apply_url_contract(target)
    if bool(target.job_attribution_field) != bool(target.job_attribution_regex):
        raise AdapterError(
            "Greenhouse per-job attribution requires both field and regex"
        )
    attribution_regex: re.Pattern[str] | None = None
    if target.job_attribution_regex:
        try:
            attribution_regex = re.compile(str(target.job_attribution_regex), re.IGNORECASE)
        except re.error as exc:
            raise AdapterError(f"Greenhouse attribution regex is invalid: {exc}") from exc
    exclusion_regex: re.Pattern[str] | None = None
    if target.job_attribution_exclude_regex:
        try:
            exclusion_regex = re.compile(
                str(target.job_attribution_exclude_regex), re.IGNORECASE
            )
        except re.error as exc:
            raise AdapterError(
                f"Greenhouse attribution exclusion regex is invalid: {exc}"
            ) from exc

    base_url = f"https://boards-api.greenhouse.io/v1/boards/{target.slug}"
    metadata["pages_requested"] = 1
    data, listing_bytes, listing_streamed = _get_json(
        session,
        f"{base_url}/jobs",
        timeout,
        int(target.max_response_bytes),
        target.slug,
        "response",
    )
    if not isinstance(data, Mapping) or not isinstance(data.get("jobs"), list):
        raise AdapterError("Greenhouse response is missing its stable job_id collection")

    allowed_departments = {
        str(value).strip().casefold()
        for value in target.department_allowlist
        if str(value).strip()
    }
    allowed_job_ids: set[str] | None = None
    department_bytes = 0
    requests_fetched = 1
    expanded_listing = not listing_streamed
    if allowed_departments and not expanded_listing:
        department_data, department_bytes, _ = _get_json(
            session,
            f"{base_url}/departments",
            timeout,
            int(target.max_response_bytes),
            target.slug,
            "department response",
        )
        requests_fetched += 1
        allowed_job_ids = _department_job_ids(department_data, allowed_departments)

    maximum_results = int(target.max_jobs)
    detail_limit = min(int(target.max_response_bytes), MAX_DETAIL_RESPONSE_BYTES)
    summaries: list[tuple[Mapping[str, Any], JobRecord]] = []
    attribution_filtered = 0
    for item in data["jobs"]:
        summary = _listing_record(item, target, metadata, apply_url_contract)
        if allowed_job_ids is not None and summary.job_id not in allowed_job_ids:
            continue
        if attribution_regex and not _has_exact_attribution(
            summary,
            str(target.job_attribution_field),
            attribution_regex,
            exclusion_regex,
        ):
            attribution_filtered += 1
            continue
        summaries.append((item, summary))
    ids = [summary.job_id.casefold() for _item, summary in summaries]
    urls = [summary.url.casefold() for _item, summary in summaries]
    if len(set(ids)) != len(ids) or len(set(urls)) != len(urls):
        metadata["duplicate_ids"] = len(set(ids)) != len(ids)
        metadata["duplicate_canonical_urls"] = len(set(urls)) != len(urls)
        raise AdapterError("Greenhouse board repeated a stable ID or canonical URL")
    if target.endpoint_variant == STRICT_BOARD_VARIANT:
        source_identity_diagnostic = (
            "Greenhouse API rows matched the configured board slug, stable IDs, "
            "and exact allow-listed board URLs"
        )
        if apply_url_contract:
            source_identity_diagnostic = (
                "Greenhouse API rows matched the configured board slug and exact "
                "allow-listed apply URL template with stable IDs correlated in "
                "its configured path/query contract"
            )
        metadata.update(
            source_identity_status="validated",
            source_identity_diagnostic=source_identity_diagnostic,
        )
    metadata["pages_completed"] = 1

    prioritized_summaries, candidate_count = _prioritized_summaries(
        summaries, maximum_results
    )
    if target.job_attribution_field:
        prioritized_summaries = _prioritize_attribution_groups(
            prioritized_summaries,
            candidate_count,
            str(target.job_attribution_field),
        )
    selected_summaries = prioritized_summaries[:maximum_results]
    inventory_items = tuple(
        InventoryItem(summary.job_id, summary.source)
        for _item, summary in summaries
    )
    safety_limit_reached = len(summaries) >= maximum_results
    detail_priority, _ = _prioritized_summaries(
        selected_summaries, MAX_DETAIL_REQUESTS
    )
    detail_candidate_ids = {
        summary.job_id
        for _item, summary in detail_priority[:MAX_DETAIL_REQUESTS]
    }
    jobs: list[JobRecord] = []
    item_failures: list[ItemFailure] = []
    detail_requests = 0
    detail_bytes = 0
    for item, summary in selected_summaries:
        detail = _embedded_detail(item, assume_embedded=expanded_listing)
        separately_fetched_detail = False
        if detail is None and summary.job_id in detail_candidate_ids:
            detail_requests += 1
            requests_fetched += 1
            try:
                detail, response_bytes, _ = _get_json(
                    session,
                    f"{base_url}/jobs/{summary.job_id}",
                    timeout,
                    detail_limit,
                    target.slug,
                    "detail response",
                )
                separately_fetched_detail = True
                detail_bytes += response_bytes
            except requests.RequestException as exc:
                item_failures.append(
                    ItemFailure(
                        item_id=summary.job_id,
                        reason=f"{exc.__class__.__name__}: {exc}",
                        retryable=True,
                    )
                )
                _retain_greenhouse_listing(summary, target)
                jobs.append(summary)
                continue
            except AdapterError as exc:
                item_failures.append(
                    ItemFailure(
                        item_id=summary.job_id,
                        reason=str(exc),
                        retryable=_retryable_detail_error(exc),
                        outcome=(
                            "unreadable"
                            if _retryable_detail_error(exc)
                            else "rejected"
                        ),
                    )
                )
                _retain_greenhouse_listing(summary, target)
                jobs.append(summary)
                continue
        if detail is None:
            _retain_greenhouse_listing(summary, target)
            jobs.append(summary)
            continue
        try:
            if not isinstance(detail, Mapping):
                raise AdapterError("Greenhouse job detail must be a JSON object")
            detail_job_id = str(detail.get("id") or "").strip()
            if detail_job_id != summary.job_id:
                raise AdapterError(
                    "Greenhouse job detail does not match its stable job ID"
                )

            departments = _names(detail.get("departments"))
            if allowed_departments and not any(
                department.casefold() in allowed_departments
                for department in departments
            ):
                continue
            offices = _names(detail.get("offices"))
            jobs.append(
                JobRecord(
                    job_id=summary.job_id,
                    title=summary.title,
                    company=summary.company,
                    location=summary.location,
                    url=summary.url,
                    updated_at=summary.updated_at,
                    source=summary.source,
                    departments=departments,
                    offices=offices,
                    description=detail.get("content"),
                    raw_metadata=summary.raw_metadata,
                )
            )
        except AdapterError as exc:
            if not separately_fetched_detail:
                raise
            item_failures.append(
                ItemFailure(
                    item_id=summary.job_id,
                    reason=str(exc),
                    outcome="rejected",
                )
            )
            _retain_greenhouse_listing(summary, target)
            jobs.append(summary)

    setattr(
        session,
        "greenhouse_pagination",
        {
            "pages_fetched": 1,
            "maximum_pages": 1,
            "maximum_results": maximum_results,
            "jobs_returned": len(data["jobs"]),
            "jobs_scanned": len(data["jobs"]),
            "candidate_jobs": candidate_count,
            "candidates_selected": len(selected_summaries),
            "jobs_normalized": len(jobs),
            "department_allowlist": list(target.department_allowlist),
            "job_attribution_field": target.job_attribution_field,
            "attribution_filtered": attribution_filtered,
            "zero_job_contract": not data["jobs"],
            "stop_reason": (
                "maximum_results_reached"
                if safety_limit_reached
                else "complete_collection_response"
            ),
            "listing_requests": 1,
            "department_requests": int(bool(allowed_departments) and not expanded_listing),
            "detail_requests": detail_requests,
            "maximum_detail_requests": MAX_DETAIL_REQUESTS,
            "requests_fetched": requests_fetched,
            "listing_bytes": listing_bytes,
            "department_bytes": department_bytes,
            "detail_bytes": detail_bytes,
        },
    )
    metadata.update(
        scan_complete=not safety_limit_reached,
        scan_completeness_reason=(
            "safety_limit_reached"
            if safety_limit_reached
            else "complete_collection_response"
        ),
        total_available=len(summaries),
        stop_reason=(
            "maximum_results_reached"
            if safety_limit_reached
            else "complete_collection_response"
        ),
        safety_limit_reached=safety_limit_reached,
    )
    return AdapterResult(
        jobs=jobs,
        scan=metadata,
        inventory_items=inventory_items,
        item_failures=tuple(item_failures),
        capabilities_used=frozenset(
            {"inventory", "hydration", "partial_progress", "per_item_failure"}
        ),
    )


def _retain_greenhouse_listing(summary: JobRecord, target: CompanyTarget) -> None:
    if target.department_allowlist and not summary.departments:
        summary.departments = list(target.department_allowlist)


def _retryable_detail_error(exc: BaseException) -> bool:
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "http 408",
            "http 425",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
        )
    )


def _has_exact_attribution(
    job: JobRecord,
    field: str,
    include: re.Pattern[str],
    exclude: re.Pattern[str] | None,
) -> bool:
    """Accept one job only when its reviewed Greenhouse metadata is exact."""
    metadata = job.raw_metadata.get("greenhouse_metadata")
    if not isinstance(metadata, Mapping):
        raise AdapterError("Greenhouse job metadata attribution must be an object")
    field_name = field.removeprefix("greenhouse_metadata.")
    value = metadata.get(field_name)
    values = value if isinstance(value, list) else [value]
    cleaned = [str(item).strip() for item in values if str(item or "").strip()]
    return bool(cleaned) and any(include.fullmatch(item) for item in cleaned) and not any(
        exclude.search(item) for item in cleaned if exclude
    )


def _get_json(
    session: requests.Session,
    url: str,
    timeout: int,
    byte_limit: int,
    slug: str,
    label: str,
) -> tuple[Any, int, bool]:
    """Read one JSON response without consuming more than its byte budget."""
    response = session.get(url, headers=HEADERS, timeout=timeout, stream=True)
    _validate_response_identity(response, url)
    if response.status_code != 200:
        close = getattr(response, "close", None)
        if callable(close):
            close()
        raise AdapterError(f"Greenhouse HTTP {response.status_code} for {slug}")

    iter_content = getattr(response, "iter_content", None)
    if callable(iter_content):
        body = bytearray()
        try:
            for chunk in iter_content(chunk_size=READ_CHUNK_BYTES):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > byte_limit:
                    raise AdapterError(f"Greenhouse {label} exceeded the byte limit")
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        try:
            value = json.loads(bytes(body))
            _reject_hidden_continuation(value)
            return value, len(body), True
        except (TypeError, ValueError, UnicodeError) as exc:
            raise AdapterError(f"Greenhouse {label} is malformed JSON: {exc}") from exc

    content = getattr(response, "content", None)
    if content is not None:
        body = bytes(content or b"")
        if len(body) > byte_limit:
            raise AdapterError(f"Greenhouse {label} exceeded the byte limit")
        try:
            value = response.json()
            _reject_hidden_continuation(value)
            return value, len(body), False
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"Greenhouse {label} is malformed JSON: {exc}") from exc

    text = getattr(response, "text", "") or ""
    encoded_size = len(str(text).encode("utf-8"))
    if encoded_size > byte_limit:
        raise AdapterError(f"Greenhouse {label} exceeded the byte limit")
    try:
        value = response.json()
        _reject_hidden_continuation(value)
        return value, encoded_size, False
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"Greenhouse {label} is malformed JSON: {exc}") from exc


def _validate_response_identity(response: Any, requested_url: str) -> None:
    """Reject unreviewed redirects before their body can assert board identity."""
    final_url = str(getattr(response, "url", "") or "")
    history_available = hasattr(response, "history")
    history = getattr(response, "history", ()) or ()
    if history or (
        history_available and final_url and final_url != requested_url
    ):
        close = getattr(response, "close", None)
        if callable(close):
            close()
        raise AdapterError("Greenhouse response violated the exact request identity")


def _reject_hidden_continuation(value: Any) -> None:
    """An atomic board response must not quietly introduce continuation state."""
    if not isinstance(value, Mapping):
        return
    direct = ("next", "next_url", "next_cursor", "next_page")
    if any(value.get(field) not in (None, "", False) for field in direct):
        raise AdapterError("Greenhouse response exposed an unsupported continuation")
    if value.get("has_more") is True:
        raise AdapterError("Greenhouse response exposed an unsupported continuation")
    pagination = value.get("pagination")
    if isinstance(pagination, Mapping) and (
        pagination.get("has_more") is True
        or any(
            pagination.get(field) not in (None, "", False)
            for field in direct
        )
    ):
        raise AdapterError("Greenhouse response exposed an unsupported continuation")


def _summary_identity(item: Any) -> tuple[str, str]:
    if not isinstance(item, Mapping):
        raise AdapterError("Greenhouse job must be a JSON object")
    job_id = str(item.get("id") or "").strip()
    title = str(item.get("title") or "").strip()
    if not job_id or not title:
        raise AdapterError("Greenhouse job is missing a stable ID or title")
    return job_id, title


def _bounded_diagnostic(value: object, limit: int) -> str:
    """Keep source-controlled failure evidence useful without echoing large fields."""
    text = str(value or "")
    return text if len(text) <= limit else f"{text[:limit]}<truncated>"


def _apply_url_contract(target: CompanyTarget) -> tuple[str, str, str] | None:
    """Return an explicitly configured exact query-ID URL contract.

    The existing board/path contract remains the default.  A Greenhouse target
    opts into this alternative only by configuring one exact apply URL template
    whose sole query value is ``{id}``. The same placeholder may occur once in
    the path when the first-party URL repeats the stable ID there.
    """
    template = str(target.apply_url_template or "").strip()
    if not template:
        return None
    if target.endpoint_variant != STRICT_BOARD_VARIANT:
        raise AdapterError(
            "Greenhouse apply_url_template requires the strict board endpoint variant"
        )
    placeholder_count = template.count(STABLE_ID_TEMPLATE_FIELD)
    if placeholder_count not in {1, 2}:
        raise AdapterError(
            "Greenhouse apply_url_template must contain {id} once in the query "
            "and at most once in the path"
        )
    remaining_template = template.replace(STABLE_ID_TEMPLATE_FIELD, "")
    if "{" in remaining_template or "}" in remaining_template:
        raise AdapterError(
            "Greenhouse apply_url_template supports only the {id} placeholder"
        )

    parsed = urlparse(template)
    allowed_hosts = {
        str(host).strip().casefold()
        for host in target.allowed_apply_hosts
        if str(host).strip()
    }
    hostname = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.netloc.casefold() != hostname
        or hostname not in allowed_hosts
        or not parsed.path.startswith("/")
        or parsed.path == "/"
        or "#" in template
    ):
        raise AdapterError(
            "Greenhouse apply_url_template must use HTTPS, one allowed hostname, "
            "an exact non-root path, and no fragment"
        )
    path_placeholder_count = parsed.path.count(STABLE_ID_TEMPLATE_FIELD)
    if (
        path_placeholder_count not in {0, 1}
        or placeholder_count != 1 + path_placeholder_count
    ):
        raise AdapterError(
            "Greenhouse apply_url_template may repeat {id} only once in its path"
        )
    try:
        query_pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError as exc:
        raise AdapterError("Greenhouse apply_url_template query is malformed") from exc
    if (
        len(query_pairs) != 1
        or query_pairs[0][1] != STABLE_ID_TEMPLATE_FIELD
        or QUERY_PARAMETER_PATTERN.fullmatch(query_pairs[0][0]) is None
        or parsed.query != f"{query_pairs[0][0]}={STABLE_ID_TEMPLATE_FIELD}"
    ):
        raise AdapterError(
            "Greenhouse apply_url_template must have exactly one safe query parameter "
            "whose complete value is {id}"
        )
    return hostname, parsed.path, query_pairs[0][0]


def _url_identity_violations(
    parsed: Any,
    *,
    allowed_hosts: set[str],
    path_matches: bool,
    job_id: str,
    query_parameter: str | None = None,
    stable_id_valid: bool = True,
    fragment_present: bool | None = None,
) -> list[str]:
    violations: list[str] = []
    if parsed.scheme != "https":
        violations.append("scheme_not_https")
    actual_hostname = (parsed.hostname or "").casefold()
    if (
        actual_hostname not in allowed_hosts
        or (
            query_parameter is not None
            and parsed.netloc.casefold() != actual_hostname
        )
    ):
        violations.append("hostname_not_allowed")
    if not path_matches:
        violations.append("path_does_not_match_board_and_stable_id")
    if query_parameter is None:
        if parsed.query:
            violations.append("query_not_permitted")
    else:
        if not stable_id_valid:
            violations.append("stable_job_id_invalid")
        try:
            query_pairs = parse_qsl(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
            )
        except ValueError:
            query_pairs = []
        if (
            query_pairs != [(query_parameter, job_id)]
            or parsed.query != f"{query_parameter}={job_id}"
        ):
            violations.append("query_does_not_match_stable_id")
    has_fragment = (
        fragment_present if fragment_present is not None else bool(parsed.fragment)
    )
    if has_fragment:
        violations.append("fragment_not_permitted")
    return violations


def _record_url_identity_failure(
    metadata: SourceScanMetadata,
    *,
    target: CompanyTarget,
    job_id: str,
    absolute_url: str,
    parsed: Any,
    allowed_hosts: set[str],
    expected_path: str,
    violations: list[str],
    query_parameter: str | None = None,
    fragment_present: bool | None = None,
) -> None:
    """Publish bounded rejected-row evidence while preserving fail-closed behavior."""
    hosts = sorted(_bounded_diagnostic(host, 253) for host in allowed_hosts)[:20]
    path_segments = [part for part in parsed.path.split("/") if part]
    folded_path_segments = {part.casefold() for part in path_segments}
    query = parse_qs(parsed.query, keep_blank_values=True)
    query_names = sorted(_bounded_diagnostic(name, 64) for name in query)[:20]
    id_query_names = sorted(
        _bounded_diagnostic(name, 64)
        for name, values in query.items()
        if any(str(value).casefold() == job_id.casefold() for value in values)
    )[:20]
    board_token_in_path = str(target.slug).casefold() in folded_path_segments
    stable_job_id_in_path = job_id.casefold() in folded_path_segments
    expected_suffix = (
        f"?{query_parameter}={job_id}" if query_parameter is not None else ""
    )
    failure = {
        "offending_job_id": _bounded_diagnostic(job_id, 128),
        "offending_returned_job_url": _bounded_diagnostic(absolute_url, 1000),
        "expected_board_token": _bounded_diagnostic(target.slug, 128),
        "expected_hostnames": hosts,
        "expected_path": _bounded_diagnostic(expected_path, 500),
        "expected_url_patterns": [
            _bounded_diagnostic(
                f"https://{host}{expected_path}{expected_suffix}", 1000
            )
            for host in hosts
        ],
        "actual_scheme": _bounded_diagnostic(parsed.scheme, 32),
        "actual_hostname": _bounded_diagnostic(parsed.hostname, 253),
        "actual_path": _bounded_diagnostic(parsed.path, 500),
        "actual_query_present": bool(parsed.query),
        "actual_query_parameter_names": query_names,
        "actual_fragment_present": (
            fragment_present
            if fragment_present is not None
            else bool(parsed.fragment)
        ),
        "board_token_path_segment_present": board_token_in_path,
        "stable_job_id_path_segment_present": stable_job_id_in_path,
        "stable_job_id_query_parameters": id_query_names,
        "violations": violations,
    }
    if query_parameter is not None:
        failure["expected_stable_id_query_parameter"] = _bounded_diagnostic(
            query_parameter, 64
        )
    metadata.update(
        source_identity_status="failed",
        source_identity_diagnostic=(
            "Greenhouse absolute_url did not bind the configured board token and "
            "the API row's stable job ID"
        ),
        rejected_record_count=int(metadata.get("rejected_record_count") or 0) + 1,
        rejected_record_reason="greenhouse_job_url_identity_mismatch",
        greenhouse_url_identity_failure=failure,
        stop_reason="identity_failure",
    )


def _listing_record(
    item: Any,
    target: CompanyTarget,
    metadata: SourceScanMetadata,
    apply_url_contract: tuple[str, str, str] | None,
) -> JobRecord:
    """Validate and normalize the fields available on the compact listing."""
    job_id, title = _summary_identity(item)
    raw_job_id = item.get("id")
    location = item.get("location") or {}
    if not isinstance(location, Mapping):
        raise AdapterError("Greenhouse job location must be an object")
    raw_absolute_url = str(item.get("absolute_url") or "")
    absolute_url = (
        raw_absolute_url
        if apply_url_contract is not None
        else _secure_apply_url(raw_absolute_url)
    )
    try:
        parsed = urlparse(absolute_url)
        hostname = (parsed.hostname or "").casefold()
        explicit_port = parsed.port
    except ValueError as exc:
        raise AdapterError("Greenhouse canonical job URL is malformed") from exc
    if target.endpoint_variant != STRICT_BOARD_VARIANT and (
        parsed.scheme != "https"
        or not hostname
        or explicit_port is not None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise AdapterError("Greenhouse canonical job URL is malformed")
    if target.endpoint_variant == STRICT_BOARD_VARIANT:
        parts = [part for part in parsed.path.split("/") if part]
        query_parameter: str | None = None
        if apply_url_contract is None:
            allowed = {str(host).casefold() for host in target.allowed_apply_hosts}
            expected_path = f"/{target.slug}/jobs/{job_id}"
            path_matches = (
                len(parts) == 3
                and parts[0].casefold() == str(target.slug).casefold()
                and parts[1].casefold() == "jobs"
                and parts[2].casefold() == job_id.casefold()
            )
        else:
            expected_host, path_template, query_parameter = apply_url_contract
            expected_path = path_template.replace(STABLE_ID_TEMPLATE_FIELD, job_id)
            allowed = {expected_host}
            path_matches = parsed.path == expected_path
        stable_id_valid = (
            query_parameter is None
            or (
                not isinstance(raw_job_id, bool)
                and str(raw_job_id) == job_id
                and re.fullmatch(r"[0-9]+", job_id) is not None
            )
        )
        fragment_present = bool(parsed.fragment) or (
            query_parameter is not None and "#" in absolute_url
        )
        violations = _url_identity_violations(
            parsed,
            allowed_hosts=allowed,
            path_matches=path_matches,
            job_id=job_id,
            query_parameter=query_parameter,
            stable_id_valid=stable_id_valid,
            fragment_present=fragment_present,
        )
        if violations:
            _record_url_identity_failure(
                metadata,
                target=target,
                job_id=job_id,
                absolute_url=absolute_url,
                parsed=parsed,
                allowed_hosts=allowed,
                expected_path=expected_path,
                violations=violations,
                query_parameter=query_parameter,
                fragment_present=fragment_present,
            )
            raise AdapterError("Greenhouse job URL does not match its exact board and stable ID")
    return JobRecord(
        job_id=job_id,
        title=title,
        company=target.name,
        location=location.get("name", "") or "",
        url=absolute_url,
        updated_at=item.get("updated_at", ""),
        source=f"greenhouse:{target.slug}",
        raw_metadata=_listing_metadata(item),
    )


def _prioritized_summaries(
    summaries: list[tuple[Mapping[str, Any], JobRecord]], maximum_results: int
) -> tuple[list[tuple[Mapping[str, Any], JobRecord]], int]:
    """Search a full large board and prioritize likely in-scope student roles."""
    if len(summaries) <= maximum_results:
        return summaries, len(summaries)

    candidates: list[tuple[tuple[Mapping[str, Any], JobRecord], Mapping[str, Any]]] = []
    for pair in summaries:
        signals = student_job_signals(pair[1])
        if signals["matching_rule"]:
            candidates.append((pair, signals))
    candidates.sort(
        key=lambda candidate: (
            _candidate_signal_priority(candidate[1]),
            _candidate_location_priority(candidate[0][1]),
        )
    )
    prioritized = [pair for pair, _signals in candidates]
    candidate_ids = {pair[1].job_id for pair in prioritized}
    # Preserve a useful bounded source when no internship is open today.
    # Explicit student roles stay first; ordinary roles only backfill capacity.
    prioritized.extend(pair for pair in summaries if pair[1].job_id not in candidate_ids)
    return prioritized, len(candidates)


def _prioritize_attribution_groups(
    summaries: list[tuple[Mapping[str, Any], JobRecord]],
    candidate_count: int,
    field: str,
) -> list[tuple[Mapping[str, Any], JobRecord]]:
    """Represent each accepted structured attribution without displacing students.

    A reviewed parent board can expose multiple exactly attributed brands. Keep
    all student candidates first, then reserve the earliest remaining row for
    each attribution value before backfilling in source order. This only affects
    already accepted rows and never infers a brand from title or description.
    """
    candidates = summaries[:candidate_count]
    remaining = summaries[candidate_count:]
    covered = {
        value.casefold()
        for _item, job in candidates
        for value in _attribution_values(job, field)
    }
    representatives: list[tuple[Mapping[str, Any], JobRecord]] = []
    representative_ids: set[str] = set()
    for pair in remaining:
        values = _attribution_values(pair[1], field)
        if any(value.casefold() not in covered for value in values):
            representatives.append(pair)
            representative_ids.add(pair[1].job_id)
            covered.update(value.casefold() for value in values)
    return [
        *candidates,
        *representatives,
        *(pair for pair in remaining if pair[1].job_id not in representative_ids),
    ]


def _attribution_values(job: JobRecord, field: str) -> list[str]:
    metadata = job.raw_metadata.get("greenhouse_metadata")
    if not isinstance(metadata, Mapping):
        return []
    value = metadata.get(field.removeprefix("greenhouse_metadata."))
    values = value if isinstance(value, list) else [value]
    return [str(item).strip() for item in values if str(item or "").strip()]


def _candidate_signal_priority(signals: Mapping[str, Any]) -> int:
    """Prefer explicit student markers over contextual temporary/seasonal signals."""
    if signals.get("strong_title") or signals.get("structured_strong"):
        return 0
    return 1


def _candidate_location_priority(job: JobRecord) -> int:
    """Keep source order within eligible, ambiguous, and out-of-scope groups."""
    status = classify_student_location(job)
    if status.startswith("eligible_"):
        return 0
    if status.startswith("manual_review_"):
        return 1
    return 2


def _listing_metadata(item: Mapping[str, Any]) -> dict[str, Any]:
    """Expose Greenhouse custom metadata under its field names for shared signals."""
    raw_metadata = dict(item)
    metadata = item.get("metadata")
    if metadata is None:
        raw_metadata["greenhouse_metadata"] = {}
        return raw_metadata
    if not isinstance(metadata, list):
        raise AdapterError("Greenhouse job metadata must be a list")

    named_metadata: dict[str, Any] = {}
    for field in metadata:
        if not isinstance(field, Mapping):
            raise AdapterError("Greenhouse job metadata field must be an object")
        name = str(field.get("name") or "").strip()
        if not name:
            raise AdapterError("Greenhouse job metadata field is missing its name")
        value = field.get("value")
        if name not in named_metadata:
            named_metadata[name] = value
        else:
            existing = named_metadata[name]
            named_metadata[name] = (
                [*existing, value] if isinstance(existing, list) else [existing, value]
            )
    raw_metadata["greenhouse_metadata"] = named_metadata
    return raw_metadata


def _embedded_detail(
    item: Mapping[str, Any], *, assume_embedded: bool = False
) -> Mapping[str, Any] | None:
    """Retain compatibility with already-expanded fixtures and cached responses."""
    detail_fields = ("content", "departments", "offices")
    for field in ("departments", "offices"):
        if field in item:
            _names(item.get(field))
    if assume_embedded or all(field in item for field in detail_fields):
        return item
    return None


def _department_job_ids(data: Any, allowed_departments: set[str]) -> set[str]:
    if not isinstance(data, Mapping) or not isinstance(data.get("departments"), list):
        raise AdapterError("Greenhouse department response is missing its department collection")
    job_ids: set[str] = set()
    for department in data["departments"]:
        if not isinstance(department, Mapping):
            raise AdapterError("Greenhouse department must be a JSON object")
        name = str(department.get("name") or "").strip()
        department_jobs = department.get("jobs")
        if not isinstance(department_jobs, list):
            raise AdapterError("Greenhouse department jobs must be a list")
        if name.casefold() not in allowed_departments:
            continue
        for item in department_jobs:
            job_id, _ = _summary_identity(item)
            job_ids.add(job_id)
    return job_ids


def _names(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AdapterError("Greenhouse departments/offices must be lists")
    names: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise AdapterError("Greenhouse department/office must be an object")
        name = str(item.get("name") or "").strip()
        if name:
            names.append(name)
    return names
