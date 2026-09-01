"""Bounded adapter for Ashby's anonymous hosted job boards."""

from __future__ import annotations

import json
import re
from typing import Any, List, Mapping
from urllib.parse import urlparse

import requests

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord
from src.scraper.company_careers.student_roles import (
    classify_student_location,
    student_job_signals,
)


MAX_RESULTS = 20
MAX_RESPONSE_BYTES = 3_000_000
MAX_DETAIL_RESPONSE_BYTES = 1_000_000
MAX_TIMEOUT_SECONDS = 60
READ_CHUNK_BYTES = 64 * 1024
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$")
APP_DATA_MARKER = "window.__appData = "
HEADERS = {"Accept": "text/html", "User-Agent": "job-radar/1.0"}


def fetch_jobs(
    session: requests.Session,
    target: CompanyTarget,
    timeout: int = 20,
) -> List[JobRecord]:
    """Scan the compact full-board index and enrich at most ``max_jobs`` jobs."""
    _validate_target(target)
    board_url = f"https://jobs.ashbyhq.com/{target.slug}"
    document, listing_bytes = _get_document(
        session,
        board_url,
        timeout,
        int(target.max_response_bytes),
        target.slug,
        "response",
    )

    # Retain compatibility with older cached fixtures. Production hosted-board
    # responses are HTML and always take the compact-index path below.
    legacy_data = _legacy_payload(document)
    if legacy_data is not None:
        return _legacy_jobs(session, target, legacy_data, listing_bytes)

    app_data = _app_data(document, "response")
    _validate_board_identity(app_data, target, "response")
    job_board = app_data.get("jobBoard")
    if not isinstance(job_board, Mapping) or not isinstance(
        job_board.get("jobPostings"), list
    ):
        raise AdapterError("Ashby response is missing its compact job collection")

    teams = _team_lookup(job_board.get("teams"))
    summaries = [
        (item, _listing_record(item, target, teams))
        for item in job_board["jobPostings"]
    ]
    _reject_duplicate_ids([record for _item, record in summaries])

    maximum_results = int(target.max_jobs)
    prioritized, candidate_count = _prioritized_pairs(summaries, maximum_results)
    selected = prioritized[:maximum_results]
    jobs: list[JobRecord] = []
    detail_bytes = 0
    detail_limit = min(
        int(target.max_response_bytes), MAX_DETAIL_RESPONSE_BYTES
    )
    for _item, summary in selected:
        detail_document, response_bytes = _get_document(
            session,
            summary.url,
            timeout,
            detail_limit,
            target.slug,
            "detail response",
        )
        detail_bytes += response_bytes
        detail_data = _app_data(detail_document, "detail response")
        _validate_board_identity(detail_data, target, "detail response")
        jobs.append(_detail_record(detail_data.get("posting"), summary, target))

    _set_pagination(
        session,
        maximum_results=maximum_results,
        jobs_returned=len(summaries),
        candidate_count=candidate_count,
        jobs_normalized=len(jobs),
        candidates_selected=len(selected),
        listing_bytes=listing_bytes,
        detail_bytes=detail_bytes,
        detail_requests=len(selected),
        transport="hosted_compact_index",
    )
    return jobs


def _get_document(
    session: requests.Session,
    url: str,
    timeout: int,
    byte_limit: int,
    slug: str,
    label: str,
) -> tuple[str, int]:
    """Read one UTF-8 response without consuming past its byte budget."""
    response = session.get(
        url,
        headers=HEADERS,
        timeout=min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS),
        stream=True,
    )
    if response.status_code != 200:
        _close(response)
        raise AdapterError(f"Ashby HTTP {response.status_code} for {slug}")

    iter_content = getattr(response, "iter_content", None)
    if callable(iter_content):
        body = bytearray()
        try:
            for chunk in iter_content(chunk_size=READ_CHUNK_BYTES):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > byte_limit:
                    raise AdapterError(f"Ashby {label} exceeded the byte limit")
        finally:
            _close(response)
        raw = bytes(body)
    else:
        content = getattr(response, "content", None)
        if content is not None:
            raw = bytes(content or b"")
        elif hasattr(response, "text"):
            raw = str(getattr(response, "text", "") or "").encode("utf-8")
        else:
            # Older validator fixtures expose only ``json()``. Real requests
            # responses always use the streamed branch above.
            try:
                raw = json.dumps(
                    response.json(), separators=(",", ":")
                ).encode("utf-8")
            except (AttributeError, TypeError, ValueError) as exc:
                _close(response)
                raise AdapterError(
                    f"Ashby {label} has no readable response body: {exc}"
                ) from exc
        if len(raw) > byte_limit:
            _close(response)
            raise AdapterError(f"Ashby {label} exceeded the byte limit")
        _close(response)

    try:
        return raw.decode("utf-8"), len(raw)
    except UnicodeError as exc:
        raise AdapterError(f"Ashby {label} is not valid UTF-8: {exc}") from exc


def _close(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _legacy_payload(document: str) -> Mapping[str, Any] | None:
    stripped = document.lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    if isinstance(data, Mapping) and isinstance(data.get("jobs"), list):
        return data
    return None


def _legacy_jobs(
    session: requests.Session,
    target: CompanyTarget,
    data: Mapping[str, Any],
    listing_bytes: int,
) -> list[JobRecord]:
    normalized = [_normalize_legacy(item, target) for item in data["jobs"]]
    _reject_duplicate_ids(normalized)
    maximum_results = int(target.max_jobs)
    prioritized, candidate_count = _prioritized(normalized, maximum_results)
    jobs = prioritized[:maximum_results]
    _set_pagination(
        session,
        maximum_results=maximum_results,
        jobs_returned=len(normalized),
        candidate_count=candidate_count,
        jobs_normalized=len(jobs),
        candidates_selected=len(jobs),
        listing_bytes=listing_bytes,
        detail_bytes=0,
        detail_requests=0,
        transport="legacy_expanded_fixture",
    )
    return jobs


def _app_data(document: str, label: str) -> Mapping[str, Any]:
    marker_offset = document.find(APP_DATA_MARKER)
    if marker_offset < 0:
        raise AdapterError(f"Ashby {label} is missing compact application data")
    payload = document[marker_offset + len(APP_DATA_MARKER) :]
    try:
        data, _end = json.JSONDecoder().raw_decode(payload)
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"Ashby {label} has malformed application data: {exc}") from exc
    if not isinstance(data, Mapping):
        raise AdapterError(f"Ashby {label} application data must be an object")
    return data


def _validate_board_identity(
    data: Mapping[str, Any], target: CompanyTarget, label: str
) -> None:
    organization = data.get("organization")
    if not isinstance(organization, Mapping):
        raise AdapterError(f"Ashby {label} is missing board attribution")
    hosted_slug = str(organization.get("hostedJobsPageSlug") or "").strip()
    if not hosted_slug or hosted_slug.casefold() != str(target.slug).casefold():
        raise AdapterError(f"Ashby {label} does not match the requested board")


def _team_lookup(value: Any) -> dict[str, str]:
    if not isinstance(value, list):
        raise AdapterError("Ashby response is missing its team collection")
    teams: dict[str, str] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise AdapterError("Ashby team must be a JSON object")
        team_id = str(item.get("id") or "").strip()
        name = str(item.get("externalName") or item.get("name") or "").strip()
        if not ID_RE.fullmatch(team_id) or not name:
            raise AdapterError("Ashby team is missing a stable ID or name")
        folded = team_id.casefold()
        if folded in teams:
            raise AdapterError("Ashby response contains duplicate stable team IDs")
        teams[folded] = name
    return teams


def _listing_record(
    item: Any, target: CompanyTarget, teams: Mapping[str, str]
) -> JobRecord:
    if not isinstance(item, Mapping):
        raise AdapterError("Ashby job must be a JSON object")
    job_id = str(item.get("id") or "").strip()
    title = str(item.get("title") or "").strip()
    if not ID_RE.fullmatch(job_id) or not title:
        raise AdapterError("Ashby job is missing a stable ID or title")

    team_id = str(item.get("teamId") or "").strip()
    departments: list[str] = []
    if team_id:
        team_name = teams.get(team_id.casefold())
        if not team_name:
            raise AdapterError("Ashby job refers to an unknown stable team ID")
        departments.append(team_name)

    locations = _listing_locations(item)
    url = f"https://jobs.ashbyhq.com/{target.slug}/{job_id}"
    return JobRecord(
        job_id=job_id,
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=url,
        updated_at="",
        source=f"ashby:{target.slug}",
        departments=departments,
        offices=locations,
        raw_metadata={
            "platform": "ashby",
            "source_posted_at": item.get("publishedDate") or item.get("publishedAt"),
            "external_job_id": job_id,
            "employmentType": item.get("employmentType"),
            "workplaceType": item.get("workplaceType"),
            "teamId": team_id or None,
        },
    )


def _listing_locations(item: Mapping[str, Any]) -> list[str]:
    locations: list[str] = []
    primary = str(item.get("locationName") or "").strip()
    if primary:
        locations.append(primary)
    secondary = item.get("secondaryLocations")
    if not isinstance(secondary, list):
        raise AdapterError("Ashby job secondaryLocations must be a list")
    for value in secondary:
        if not isinstance(value, Mapping):
            raise AdapterError("Ashby secondary location must be a JSON object")
        name = str(value.get("locationName") or "").strip()
        if not name:
            raise AdapterError("Ashby secondary location is missing its name")
        locations.append(name)
    return _unique(locations)


def _detail_record(
    posting: Any, summary: JobRecord, target: CompanyTarget
) -> JobRecord:
    if not isinstance(posting, Mapping):
        raise AdapterError("Ashby detail response is missing its posting")
    job_id = str(posting.get("id") or "").strip()
    title = str(posting.get("title") or "").strip()
    if not ID_RE.fullmatch(job_id) or not title:
        raise AdapterError("Ashby detail response is missing a stable ID or title")
    if job_id.casefold() != summary.job_id.casefold():
        raise AdapterError("Ashby job detail does not match its stable job ID")
    if posting.get("isConfidential") is True or posting.get("isListed") is False:
        raise AdapterError("Ashby job detail is not a public listed posting")

    description = posting.get("descriptionHtml")
    if description is not None and not isinstance(description, str):
        raise AdapterError("Ashby job detail description must be text")
    department = str(
        posting.get("departmentExternalName")
        or posting.get("departmentName")
        or ""
    ).strip()
    secondary_locations = _string_list(
        posting.get("secondaryLocationNames"), "secondary location"
    )
    primary_location = str(posting.get("locationName") or "").strip()
    offices = _unique([primary_location, *secondary_locations])
    raw_metadata = dict(summary.raw_metadata)
    raw_metadata.update(
        {
            "employmentType": posting.get("employmentType"),
            "workplaceType": posting.get("workplaceType"),
            "teamNames": _string_list(posting.get("teamNames"), "team"),
        }
    )
    return JobRecord(
        job_id=summary.job_id,
        title=summary.title,
        company=summary.company,
        location=summary.location,
        url=summary.url,
        updated_at=summary.updated_at,
        source=f"ashby:{target.slug}",
        departments=[department] if department else summary.departments,
        offices=offices or summary.offices,
        description=description,
        raw_metadata=raw_metadata,
    )


def _string_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AdapterError(f"Ashby job detail {label}s must be a list")
    result: list[str] = []
    for item in value:
        cleaned = str(item or "").strip()
        if not cleaned:
            raise AdapterError(f"Ashby job detail {label} is empty")
        result.append(cleaned)
    return _unique(result)


def _validate_target(target: CompanyTarget) -> None:
    if not target.slug or not SLUG_RE.fullmatch(str(target.slug)):
        raise AdapterError("Ashby target is missing a valid slug")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError(f"Ashby max_jobs cannot exceed {MAX_RESULTS}")
    if not 1 <= int(target.max_response_bytes) <= MAX_RESPONSE_BYTES:
        raise AdapterError("Ashby response byte limit exceeds the hard maximum")
    if target.allowed_apply_hosts and {
        str(host).casefold() for host in target.allowed_apply_hosts
    } != {"jobs.ashbyhq.com"}:
        raise AdapterError(
            "Ashby allowed_apply_hosts must contain only jobs.ashbyhq.com"
        )


def _normalize_legacy(item: Any, target: CompanyTarget) -> JobRecord:
    if not isinstance(item, Mapping):
        raise AdapterError("Ashby job must be a JSON object")
    job_id = str(item.get("id") or "").strip()
    title = str(item.get("title") or "").strip()
    job_url = str(item.get("jobUrl") or "").strip()
    parsed = urlparse(job_url)
    parts = [part for part in parsed.path.split("/") if part]
    if not ID_RE.fullmatch(job_id) or not title:
        raise AdapterError("Ashby job is missing a stable ID or title")
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != "jobs.ashbyhq.com"
        or len(parts) != 2
        or parts[0].casefold() != str(target.slug).casefold()
        or parts[1].casefold() != job_id.casefold()
    ):
        raise AdapterError("Ashby job has no exact board-owned stable URL")
    location = str(item.get("location") or "").strip()
    return JobRecord(
        job_id=job_id,
        title=title,
        company=target.name,
        location=location,
        url=job_url,
        updated_at=str(item.get("publishedDate") or item.get("publishedAt") or ""),
        source=f"ashby:{target.slug}",
        departments=[str(item.get("department"))] if item.get("department") else [],
        offices=[location] if location else [],
        description=item.get("descriptionPlain") or item.get("description"),
        raw_metadata={
            "platform": "ashby",
            "external_job_id": job_id,
            "employmentType": item.get("employmentType"),
            "workplaceType": item.get("workplaceType"),
        },
    )


def _reject_duplicate_ids(jobs: list[JobRecord]) -> None:
    if len({job.job_id.casefold() for job in jobs}) != len(jobs):
        raise AdapterError("Ashby response contains duplicate stable job IDs")


def _prioritized_pairs(
    jobs: list[tuple[Mapping[str, Any], JobRecord]], maximum_results: int
) -> tuple[list[tuple[Mapping[str, Any], JobRecord]], int]:
    records, candidate_count = _prioritized(
        [record for _item, record in jobs], maximum_results
    )
    by_id = {record.job_id.casefold(): (item, record) for item, record in jobs}
    return [by_id[record.job_id.casefold()] for record in records], candidate_count


def _prioritized(
    jobs: list[JobRecord], maximum_results: int
) -> tuple[list[JobRecord], int]:
    if len(jobs) <= maximum_results:
        return jobs, len(jobs)
    candidates = [(job, student_job_signals(job)) for job in jobs]
    candidates = [pair for pair in candidates if pair[1]["matching_rule"]]
    candidates.sort(
        key=lambda pair: (
            0
            if pair[1].get("strong_title") or pair[1].get("structured_strong")
            else 1,
            _location_priority(pair[0]),
        )
    )
    prioritized = [job for job, _signals in candidates]
    ids = {job.job_id.casefold() for job in prioritized}
    prioritized.extend(job for job in jobs if job.job_id.casefold() not in ids)
    return prioritized, len(candidates)


def _location_priority(job: JobRecord) -> int:
    status = classify_student_location(job)
    if status.startswith("eligible_"):
        return 0
    if status.startswith("manual_review_"):
        return 1
    return 2


def _set_pagination(
    session: requests.Session,
    *,
    maximum_results: int,
    jobs_returned: int,
    candidate_count: int,
    jobs_normalized: int,
    candidates_selected: int,
    listing_bytes: int,
    detail_bytes: int,
    detail_requests: int,
    transport: str,
) -> None:
    setattr(
        session,
        "ashby_pagination",
        {
            "pages_fetched": 1,
            "maximum_pages": 1,
            "maximum_results": maximum_results,
            "jobs_returned": jobs_returned,
            "jobs_scanned": jobs_returned,
            "candidate_jobs": candidate_count,
            "candidates_selected": candidates_selected,
            "jobs_normalized": jobs_normalized,
            "zero_job_contract": not jobs_returned,
            "stop_reason": (
                "validated_zero_jobs"
                if not jobs_returned
                else "maximum_results_reached"
                if jobs_returned > jobs_normalized
                else "bounded_collection"
            ),
            "listing_requests": 1,
            "detail_requests": detail_requests,
            "requests_fetched": 1 + detail_requests,
            "listing_bytes": listing_bytes,
            "detail_bytes": detail_bytes,
            "transport": transport,
        },
    )


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
