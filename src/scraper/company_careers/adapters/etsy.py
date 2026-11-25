"""Enumerate the reviewed Etsy search ranges and recheck the first page."""

import re
from urllib.parse import urljoin, urlsplit

import requests
from lxml import html

from ..listing_completion import finish_authoritative_total
from ..models import AdapterError, SourceScanMetadata, publish_source_scan_metadata


def listing_root(response, target):
    from . import static_html

    body = static_html._response_bytes(response, target)
    if getattr(response, "_job_radar_html_truncated", False):
        raise AdapterError("Incomplete HTML listing body")
    encoding = requests.utils.get_encoding_from_headers(response.headers)
    if encoding and "charset=" in response.headers.get("Content-Type", "").lower():
        return html.fromstring(
            body, parser=html.HTMLParser(encoding=encoding), base_url=response.url
        )
    return static_html._parse_html(body, response)


def etsy_page(session, target, number, timeout):
    from . import static_html

    url = target.pagination_url_template.format(page=number)
    response = static_html._get(session, url, timeout)
    if response.url != url or response.history:
        raise AdapterError("Etsy listing redirected")
    root = listing_root(response, target)
    if (
        root.xpath("normalize-space(//title)") != "Search Page"
        or urlsplit(url).netloc != "careers.etsy.com"
    ):
        raise AdapterError("Etsy search identity changed")
    ranges = set()
    for node in root.xpath("//*[@aria-live]"):
        value = " ".join(" ".join(node.itertext()).split())
        match = re.fullmatch(
            r"Displaying\s+([\d,]+)\s*[-–]\s*([\d,]+)\s+of\s+([\d,]+)\s+in total",
            value.strip(),
            re.I,
        )
        if match:
            ranges.add(tuple(int(x.replace(",", "")) for x in match.groups()))
    if len(ranges) != 1:
        raise AdapterError("Etsy search range missing or contradictory")
    containers = static_html.select(root, target.listing_selector)
    cards = static_html.select(root, target.job_card_selector)
    records = static_html._listing_records(containers, cards, target, url)
    jobs = [static_html._normalize(r, target, url) for r in records]
    if any(
        urlsplit(j.url).netloc != "careers.etsy.com"
        or not re.fullmatch(r"/jobs/[^/]+/?", urlsplit(j.url).path)
        for j in jobs
    ):
        raise AdapterError("Etsy posting crossed board")
    pages = set()
    for a in root.xpath("//a[@href]"):
        text = " ".join(a.itertext()).strip()
        if (
            text.isdigit()
            or text in ("‹", "›", "Next", "Previous", "»", "«")
            or "next" in a.get("rel", "").split()
        ):
            link = urlsplit(urljoin(url, a.get("href")))
            if (
                link.scheme != "https"
                or link.netloc != "careers.etsy.com"
                or link.path != "/jobs/search"
                or link.fragment
            ):
                raise AdapterError("Etsy pagination crossed board")
            if not link.query:
                pages.add(1)
            elif re.fullmatch(r"page=[1-9]\d*", link.query):
                pages.add(int(link.query[5:]))
            else:
                raise AdapterError("Etsy pagination changed filters")
    return ranges.pop(), pages, jobs


def fetch_jobs(session, target, timeout):
    if (
        target.name != "Etsy"
        or target.pagination_url_template
        != "https://careers.etsy.com/jobs/search?page={page}"
    ):
        raise AdapterError("Etsy source changed")
    metadata = SourceScanMetadata(
        source_identity_status="unproven", pages_completed=0, pages_requested=0
    )
    session.static_html_pagination = metadata
    publish_source_scan_metadata(session, metadata)
    jobs = []
    first = None
    for page in range(1, target.max_pages + 1):
        metadata["pages_requested"] += 1
        extent, pages, items = etsy_page(session, target, page, timeout)
        start, end, total = extent
        signature = (extent, pages, [(j.job_id, j.url) for j in items])
        if first is None:
            first = signature
        if (
            start != len(jobs) + 1
            or end != len(jobs) + len(items)
            or not items
            or total != first[0][2]
            or end > total
        ):
            raise AdapterError("Etsy listing range changed")
        jobs.extend(items)
        if (
            len(jobs) > target.max_jobs
            or len({j.job_id for j in jobs}) != len(jobs)
            or len({j.url for j in jobs}) != len(jobs)
        ):
            raise AdapterError("Etsy inventory exceeds bound or repeats an identity")
        metadata.update(
            source_identity_status="validated",
            pages_completed=page,
            pages_fetched=page,
            total_available=total,
            jobs_normalized=len(jobs),
        )
        if end == total and not any(n > page for n in pages):
            metadata["pages_requested"] += 1
            extent, pages, items = etsy_page(session, target, 1, timeout)
            if (extent, pages, [(j.job_id, j.url) for j in items]) != first:
                raise AdapterError("Etsy initial page changed during enumeration")
            finish_authoritative_total(
                metadata, total, len(jobs), prior_pagination=page > 1
            )
            return jobs
        if page + 1 not in pages or end >= total:
            raise AdapterError("Etsy continuation contradicts range")
    raise AdapterError("Etsy listing reached safety bound")
