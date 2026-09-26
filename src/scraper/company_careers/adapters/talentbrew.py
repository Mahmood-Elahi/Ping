"""Anonymous Radancy/TalentBrew server-rendered search adapter."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

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
    enforce_pagination_fail_closed,
    finish_authoritative_total,
    html_pagination_evidence,
    pagination_terminal_accepted,
    publish_pagination_evidence,
    record_terminal_evidence,
)


ENDPOINT_VARIANT = "search_results_data_job_id_v1"
MAX_PAGES = 2
MAX_RESULTS = 20


def validate_target(target: CompanyTarget) -> None:
    missing = [name for name, value in (
        ("listing_url", target.listing_url),
        ("identity_regex", target.identity_regex),
        ("allowed_apply_hosts", target.allowed_apply_hosts),
    ) if not value]
    if missing:
        raise AdapterError(f"TalentBrew target is missing {', '.join(missing)}")
    if target.endpoint_variant != ENDPOINT_VARIANT:
        raise AdapterError(f"TalentBrew unsupported variant: {target.endpoint_variant}")
    static_html._require_https(str(target.listing_url), "listing_url")
    static_html._safe_regex(str(target.identity_regex))
    static_html._validate_hosts(target.allowed_apply_hosts)
    listing_host = (urlparse(str(target.listing_url)).hostname or "").casefold()
    if not static_html._allowed_host(listing_host, target.allowed_apply_hosts):
        raise AdapterError("TalentBrew listing host is not an allowed apply host")
    if not 1 <= int(target.max_pages) <= MAX_PAGES:
        raise AdapterError("TalentBrew page limit exceeds the hard maximum")
    if not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError("TalentBrew result limit exceeds the hard maximum")
    if not 1 <= int(target.max_response_bytes) <= static_html.MAX_RESPONSE_BYTES:
        raise AdapterError("TalentBrew response byte limit exceeds the hard maximum")


def fetch_jobs(
    session: requests.Session, target: CompanyTarget, timeout: int = 60
) -> list[JobRecord]:
    """Collect stable TalentBrew cards using the platform's data-job-id contract."""
    validate_target(target)
    maximum_pages = min(int(target.max_pages), MAX_PAGES)
    maximum_jobs = min(int(target.max_jobs), MAX_RESULTS)
    timeout = min(max(1, int(timeout)), static_html.MAX_TIMEOUT_SECONDS)
    metadata = SourceScanMetadata(**{
        "endpoint_variant": ENDPOINT_VARIANT,
        "request_method": "GET",
        "pages": [],
        "pages_fetched": 0,
        "jobs_normalized": 0,
        "maximum_pages": maximum_pages,
        "maximum_results": maximum_jobs,
        "stop_reason": "not_started",
        "zero_job_contract": False,
    })
    setattr(session, "talentbrew_pagination", metadata)
    publish_source_scan_metadata(session, metadata)
    listing_budget = AdaptiveListingCompletion(maximum_pages, maximum_jobs)
    listing_budget.record(metadata)
    listing_budget.record_terminal_probe_decision(
        metadata,
        max(1, int(target.page_size)),
        requests_completed=0,
        results_observed=0,
        deterministic_terminal_contract=True,
    )
    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    next_url = str(target.listing_url)

    for page_index in range(int(metadata["effective_listing_request_ceiling"])):
        if next_url in seen_urls:
            metadata["stop_reason"] = "repeated_page"
            break
        seen_urls.add(next_url)
        metadata["pages_requested"] += 1
        response = static_html._get(session, next_url, timeout)
        content = static_html._response_bytes(response, target)
        root = static_html._parse_html(content, response)
        _require_identity(root, target)
        evidence = html_pagination_evidence(root)
        publish_pagination_evidence(
            metadata, evidence, prior_pagination=page_index > 0
        )
        if evidence.reported_total is not None:
            metadata["total_available"] = evidence.reported_total
            metadata["authoritative_total_present"] = True
        cards = root.xpath(
            "//*[@id='search-results' or @id='search-results-list']//a[@data-job-id and @href]"
        )
        unique_cards: list[Any] = []
        page_ids: set[str] = set()
        for card in cards:
            external_id = str(card.get("data-job-id") or "").strip()
            if external_id and external_id not in page_ids:
                page_ids.add(external_id)
                unique_cards.append(card)
        page_row = {
            "page": page_index + 1,
            "url": next_url,
            "jobs_returned": len(unique_cards),
            "jobs_normalized": 0,
        }
        metadata["pages"].append(page_row)
        metadata["pages_fetched"] = len(metadata["pages"])
        metadata["pages_completed"] += 1
        if not unique_cards:
            static_html._reject_non_listing(root, content, False)

        new_jobs = 0
        for card in unique_cards:
            job = _normalize(card, target, next_url)
            if job.job_id in seen_ids:
                continue
            seen_ids.add(job.job_id)
            jobs.append(job)
            new_jobs += 1
            if len(jobs) >= int(metadata["effective_listing_job_ceiling"]):
                break
        page_row["jobs_normalized"] = new_jobs
        metadata["jobs_normalized"] = len(jobs)
        if finish_authoritative_total(
            metadata,
            evidence.reported_total,
            len(jobs),
            evidence=evidence,
            prior_pagination=page_index > 0,
        ):
            break
        if pagination_terminal_accepted(
            metadata, evidence, prior_pagination=page_index > 0
        ):
            metadata["stop_reason"] = "explicit_terminal_page"
            metadata["scan_complete"] = True
            metadata["scan_completeness_reason"] = "explicit_next_page_exhausted"
            metadata["total_available"] = len(jobs)
            record_terminal_evidence(
                metadata,
                "html_pagination_contract",
                list(evidence.fields),
                has_more=False,
                terminal_page_observed=True,
            )
            break
        if new_jobs == 0:
            metadata["stop_reason"] = "no_new_stable_ids"
            break
        links = root.xpath("//a[contains(concat(' ', normalize-space(@class), ' '), ' next ') and @href]")
        if not links or "disabled" in str(links[0].get("class") or "").split():
            if pagination_terminal_accepted(
                metadata,
                evidence,
                prior_pagination=page_index > 0,
                additional_terminal_reason="configured_next_link_absent",
            ):
                metadata["stop_reason"] = "no_next_page"
                metadata["scan_complete"] = True
                metadata["scan_completeness_reason"] = "explicit_next_page_exhausted"
                metadata["total_available"] = len(jobs)
                record_terminal_evidence(
                    metadata,
                    "configured_next_link_absent",
                    "a.next",
                    has_more=False,
                    terminal_page_observed=True,
                )
            else:
                metadata["stop_reason"] = "pagination_evidence_not_terminal"
            break
        proposed = urljoin(next_url, str(links[0].get("href")))
        static_html._require_listing_host(proposed, target)
        next_url = proposed
        if page_index + 1 < int(metadata["effective_listing_request_ceiling"]):
            time.sleep(static_html.RETRY_DELAY_SECONDS)
    else:
        metadata["stop_reason"] = "maximum_pages_reached"
    if metadata["stop_reason"] in {"maximum_results_reached", "maximum_pages_reached"}:
        metadata["scan_completeness_reason"] = "safety_limit_reached"
        metadata["safety_limit_reached"] = True
    elif not metadata["scan_complete"]:
        metadata["scan_completeness_reason"] = "pagination_failure"
    enforce_pagination_fail_closed(metadata)
    return jobs


def _require_identity(root: Any, target: CompanyTarget) -> None:
    text = " ".join(root.text_content().split())
    if not static_html._safe_regex(str(target.identity_regex)).search(text):
        static_html._reject_non_listing(root, text.encode("utf-8"), False)
        raise AdapterError("TalentBrew listing failed exact company attribution")


def _class_nodes(card: Any, *tokens: str) -> list[Any]:
    return card.xpath(
        ".//*[" + " or ".join(
            f"contains(concat(' ', normalize-space(@class), ' '), ' {token} ')"
            for token in tokens
        ) + "]"
    )


def _texts(nodes: list[Any]) -> list[str]:
    values: list[str] = []
    for node in nodes:
        value = " ".join(node.text_content().split())
        value = value.removeprefix("Location:").removeprefix("Category:").strip()
        if value and value not in values:
            values.append(value)
    return values


def _normalize(card: Any, target: CompanyTarget, page_url: str) -> JobRecord:
    external_id = str(card.get("data-job-id") or "").strip()
    titles = card.xpath(".//h2[1]")
    title = " ".join(titles[0].text_content().split()) if titles else ""
    if not external_id.isdigit() or not title:
        raise AdapterError("TalentBrew job is missing title or stable numeric ID")
    url = static_html._official_url(urljoin(page_url, str(card.get("href"))), target)
    if f"/{external_id}" not in urlparse(url).path:
        raise AdapterError("TalentBrew detail URL does not contain its stable job ID")
    locations = _texts(_class_nodes(card, "job-location", "section29__result-location"))
    if not locations:
        raise AdapterError("TalentBrew job is missing its location")
    departments = _texts(_class_nodes(card, "job-category", "section29__result-category"))
    dates = _texts(_class_nodes(card, "job-date-posted", "section29__result-date"))
    host = (urlparse(str(target.listing_url)).hostname or "").casefold()
    return JobRecord(
        job_id=f"talentbrew:{host}:{external_id}",
        title=title,
        company=target.name,
        location=" | ".join(locations),
        url=url,
        updated_at=dates[0] if dates else "",
        source=f"talentbrew:{host}",
        departments=departments,
        offices=locations,
        raw_metadata={
            "platform": "talentbrew",
            "endpoint_variant": ENDPOINT_VARIANT,
            "external_job_id": external_id,
        },
    )
