"""Opt-in counted pagination on existing employer-owned static listing contracts."""

import re
from urllib.parse import urljoin, urlsplit

from src.scraper.company_careers.listing_completion import (
    finish_authoritative_total,
    html_pagination_evidence,
    publish_pagination_evidence,
)
from src.scraper.company_careers.models import (
    AdapterError,
    SourceScanMetadata,
    publish_source_scan_metadata,
)

BOARD_IDS = {"Applied Materials": "95", "BlackRock": "45831", "Chevron": "38138"}
# Existing paths and stable-ID parsers remain the production ones. The values
# below limit a single traversal; reaching either bound cannot finish a scan.
PROFILES = {
    "Applied Materials": (
        "https://jobs.appliedmaterials.com/en/search-jobs",
        160,
        2400,
    ),
    "BlackRock": ("https://careers.blackrock.com/en/search-jobs", 50, 1000),
    "Chevron": ("https://careers.chevron.com/search-jobs", 50, 1000),
}


def bounds(target):
    profile = PROFILES.get(target.name)
    if not profile:
        raise AdapterError("Unreviewed counted Radancy board")
    url, pages, jobs = profile
    if (
        target.listing_url != url
        or target.pagination_url_template != url + "?p={page}"
        or target.source_scope != "default"
        or target.platform != "static_html"
        or set(target.allowed_apply_hosts) != {urlsplit(url).hostname}
    ):
        raise AdapterError("Counted Radancy board scope or destination changed")
    return pages, jobs


def _first_page_signature(root):
    nodes = root.xpath("//*[@id='search-results']")
    if len(nodes) != 1:
        return None
    node = nodes[0]
    counts = tuple(
        node.get(key)
        for key in (
            "data-total-results",
            "data-total-job-results",
            "data-total-pages",
            "data-current-page",
            "data-records-per-page",
        )
    )
    links = tuple(
        (link.get("data-job-id"), link.get("href"))
        for link in node.xpath(".//a[@data-job-id]")
    )
    return (
        tuple(root.xpath("//meta[@property='og:site_name']/@content")),
        counts,
        links,
        html_pagination_evidence(root),
    )


def fetch_jobs(session, target, timeout):
    bounds(target)
    from src.scraper.company_careers.adapters import static_html
    from src.scraper.company_careers.adapters.talentbrew import (
        _talentbrew_pagination_evidence,
    )

    jobs = []
    ids = set()
    urls = set()
    contract = None
    first_page = None
    metadata = SourceScanMetadata(source_identity_status="unproven")
    session.static_html_pagination = metadata
    publish_source_scan_metadata(session, metadata)
    for number in range(1, target.max_pages + 1):
        url = target.pagination_url_template.format(page=number)
        metadata["pages_requested"] += 1
        response = static_html._get(
            session, url, min(timeout, static_html.MAX_TIMEOUT_SECONDS)
        )
        content = static_html._response_bytes(response, target)
        if (
            response.url != url
            or getattr(response, "history", ())
            or getattr(response, "_job_radar_html_truncated", False)
        ):
            metadata["stop_reason"] = "redirect_or_truncated_listing"
            break
        root = static_html._parse_html(content, response)
        if number == 1:
            first_page = _first_page_signature(root)
        publishers = root.xpath("//meta[@property='og:site_name']/@content")
        names = {name.strip().casefold() for name in publishers}
        allowed = {
            target.name.casefold(),
            target.name.casefold() + " careers",
            "careers at " + target.name.casefold(),
        }

        if (names and not names.issubset(allowed)) or not radancy_signature(
            root, target
        ):
            metadata.update(
                stop_reason="employer_publisher_unproven",
                source_identity_status="failed",
            )
            break
        metadata["source_identity_status"] = "validated"
        evidence = _talentbrew_pagination_evidence(root)
        controls = html_pagination_evidence(root)
        if (
            "talentbrew_total" not in evidence.fields
            or evidence.malformed_fields
            or evidence.conflicting_fields
            or controls.malformed_fields
            or controls.conflicting_fields
            or evidence.current_page != number
            or (evidence.explicit_final_page and controls.continuation_reasons())
            or (
                not evidence.explicit_final_page
                and controls.terminal_reasons(prior_pagination=number > 1)
            )
            or (controls.current_page is not None and controls.current_page != number)
            or (
                controls.page_count is not None
                and controls.page_count != evidence.page_count
            )
            or (
                controls.end_index is not None
                and controls.end_index != evidence.end_index
            )
            or (
                controls.reported_total is not None
                and controls.reported_total != evidence.reported_total
            )
        ):
            metadata["stop_reason"] = "inconsistent_or_missing_pagination_evidence"
            break
        node = root.xpath("//*[@id='search-results']")[0]
        current = (
            evidence.reported_total,
            evidence.page_count,
            int(node.get("data-records-per-page")),
        )
        if contract and current != contract:
            metadata.update(
                stop_reason="collection_changed_during_scan",
                collection_changed_during_scan=True,
            )
            break
        contract = current
        publish_pagination_evidence(metadata, evidence, prior_pagination=number > 1)
        metadata.update(
            total_available=evidence.reported_total, authoritative_total_present=True
        )
        # Compare the provider's cardinality and IDs to the existing parser.
        # Require a positive inventory until the empty-board contract is reviewed.
        containers = static_html.select(root, target.listing_selector)
        cards = [
            card
            for card in static_html.select(root, target.job_card_selector)
            if any(static_html._contains(c, card) for c in containers)
        ]
        records = static_html._listing_records(containers, cards, target, url)
        provider_ids = node.xpath(".//a[@data-job-id]/@data-job-id")
        if not records or len(records) != evidence.result_count:
            metadata["stop_reason"] = "page_count_mismatch"
            break
        page_jobs = [static_html._normalize(record, target, url) for record in records]
        page_ids = {job.job_id for job in page_jobs}
        page_urls = {job.url for job in page_jobs}
        if (
            len(page_ids) != len(records)
            or len(page_urls) != len(records)
            or ids & page_ids
            or urls & page_urls
        ):
            metadata.update(
                stop_reason="unstable_listing_identity",
                collection_changed_during_scan=True,
            )
            break
        exact = True
        for record, job in zip(records, page_jobs, strict=True):
            external = str(job.raw_metadata["external_job_id"])
            path_id = urlsplit(job.url).path.rstrip("/").rsplit("/", 1)[-1]
            if (
                not external.isdigit()
                or path_id != external
                or {link.get("data-job-id") for link in record.links} != {external}
            ):
                exact = False
                break
        normalized_ids = {str(job.raw_metadata["external_job_id"]) for job in page_jobs}
        if not exact or sorted(provider_ids) != sorted(normalized_ids):
            metadata["stop_reason"] = "unverified_listing_id"
            break
        if len(jobs) + len(page_jobs) > target.max_jobs:
            metadata.update(
                stop_reason="maximum_results_reached", safety_limit_reached=True
            )
            break
        jobs.extend(page_jobs)
        ids.update(page_ids)
        urls.update(page_urls)
        metadata.update(
            jobs_normalized=len(jobs), pages_completed=number, pages_fetched=number
        )
        if evidence.explicit_final_page:
            if number > 1:
                first_url = target.pagination_url_template.format(page=1)
                metadata["pages_requested"] += 1
                check = static_html._get(
                    session, first_url, min(timeout, static_html.MAX_TIMEOUT_SECONDS)
                )
                body = static_html._response_bytes(check, target)
                if (
                    check.url != first_url
                    or getattr(check, "history", ())
                    or getattr(check, "_job_radar_html_truncated", False)
                    or not radancy_signature(
                        static_html._parse_html(body, check), target
                    )
                    or _first_page_signature(static_html._parse_html(body, check))
                    != first_page
                ):
                    metadata.update(
                        stop_reason="collection_changed_during_scan",
                        collection_changed_during_scan=True,
                    )
                    break
            finish_authoritative_total(
                metadata,
                evidence.reported_total,
                len(jobs),
                evidence=evidence,
                prior_pagination=number > 1,
            )
            break
    else:
        metadata.update(stop_reason="maximum_pages_reached", safety_limit_reached=True)
    if not metadata.get("scan_complete"):
        metadata["scan_completeness_reason"] = metadata["stop_reason"]
    return jobs


def radancy_signature(root, target):
    board = BOARD_IDS[target.name]
    exact_title = f"Search our Job Opportunities at {target.name}"
    if root.xpath("normalize-space(//title)") != exact_title:
        raise AdapterError("Radancy employer title mismatch")
    host = urlsplit(target.listing_url).netloc
    links = root.xpath("//*[@id='search-results']//a[@data-job-id]")
    if not links:
        raise AdapterError("No employer-bound Radancy cards")
    for node in links:
        url = urlsplit(urljoin(target.listing_url, node.get("href", "")))
        job_id = node.get("data-job-id", "")
        if not (
            url.scheme == "https"
            and url.netloc == host
            and not url.query
            and not url.fragment
            and job_id.isdecimal()
            and re.fullmatch(
                r"/(?:en/)?job/[^/]+/[^/]+/" + board + "/" + job_id + r"/?", url.path
            )
        ):
            raise AdapterError("Radancy employer board or stable posting path changed")
    return True
