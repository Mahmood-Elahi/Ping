"""Bounded, configuration-driven collector for server-rendered job lists."""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from lxml import html

from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


MAX_PAGES = 2
MAX_RESULTS = 20
MAX_ATTEMPTS = 2
MAX_TIMEOUT_SECONDS = 60
MAX_RESPONSE_BYTES = 3_000_000
RETRY_DELAY_SECONDS = 0.1
TRANSIENT_STATUSES = {202, 408, 425, 500, 502, 503, 504}
HEADERS = {"Accept": "text/html,application/xhtml+xml", "User-Agent": "job-radar/1.0"}
SUPPORTED_VARIANTS = {"server_rendered_cards", "server_rendered_links"}
_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_ATTR_RE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_.:-]*)(?:([*^$]?=)[\"']?([^\"']*)[\"']?)?$")


def fetch_jobs(session: requests.Session, target: CompanyTarget, timeout: int = 60) -> list[JobRecord]:
    """Fetch a deterministic HTML listing without browser or session state."""
    validate_target(target)
    maximum_pages = min(int(target.max_pages), MAX_PAGES)
    maximum_jobs = min(int(target.max_jobs), MAX_RESULTS)
    timeout = min(max(1, int(timeout)), MAX_TIMEOUT_SECONDS)
    metadata: dict[str, Any] = {
        "endpoint_variant": target.endpoint_variant,
        "request_method": "GET",
        "pages": [],
        "pages_fetched": 0,
        "bytes_inspected": 0,
        "jobs_normalized": 0,
        "maximum_pages": maximum_pages,
        "maximum_results": maximum_jobs,
        "stop_reason": "not_started",
        "repeated_page": False,
        "zero_job_contract": False,
        "diagnostics": [],
        "warnings": [],
    }
    setattr(session, "static_html_pagination", metadata)
    jobs: list[JobRecord] = []
    seen_ids: set[str] = set()
    seen_pages: set[tuple[str, ...]] = set()
    next_url = str(target.listing_url)

    for page_index in range(maximum_pages):
        if target.pagination_url_template:
            next_url = str(target.pagination_url_template).format(page=page_index + 1)
        response = _get(session, next_url, timeout)
        content = _response_bytes(response, target)
        metadata["bytes_inspected"] += len(content)
        root = _parse_html(content, response)
        if not select(root, str(target.listing_selector)):
            _reject_non_listing(root, content, False)
        cards = select(root, str(target.job_card_selector))
        empty = bool(target.empty_selector and select(root, str(target.empty_selector)))
        if not cards:
            _reject_non_listing(root, content, empty)
            metadata["zero_job_contract"] = empty and page_index == 0
            metadata["stop_reason"] = "validated_zero_jobs" if empty and page_index == 0 else "empty_page"
            metadata["pages"].append({"page": page_index + 1, "url": next_url, "jobs_returned": 0, "jobs_normalized": 0})
            metadata["pages_fetched"] = len(metadata["pages"])
            break

        identities = tuple(_card_identity(card, target) for card in cards)
        page_record = {"page": page_index + 1, "url": next_url, "jobs_returned": len(cards), "jobs_normalized": 0}
        metadata["pages"].append(page_record)
        metadata["pages_fetched"] = len(metadata["pages"])
        if identities in seen_pages:
            metadata["repeated_page"] = True
            metadata["stop_reason"] = "repeated_page"
            break
        seen_pages.add(identities)

        new_ids = 0
        parsed = 0
        for card in cards:
            try:
                job = _normalize(card, target, next_url)
            except AdapterError as exc:
                metadata["diagnostics"].append({"page": page_index + 1, "error": str(exc)})
                continue
            parsed += 1
            if job.job_id in seen_ids:
                continue
            seen_ids.add(job.job_id)
            jobs.append(job)
            new_ids += 1
            if len(jobs) >= maximum_jobs:
                break
        if parsed == 0:
            if metadata["diagnostics"]:
                raise AdapterError(str(metadata["diagnostics"][0]["error"]))
            raise AdapterError("Static HTML listing contained cards but none satisfied the reviewed contract")
        page_record["jobs_normalized"] = new_ids
        metadata["jobs_normalized"] = len(jobs)
        if new_ids == 0:
            metadata["stop_reason"] = "no_new_stable_ids"
            break
        if len(jobs) >= maximum_jobs:
            metadata["stop_reason"] = "maximum_results_reached"
            break
        if not target.pagination_url_template:
            if not target.next_page_selector:
                metadata["stop_reason"] = "no_pagination_configured"
                break
            links = select(root, str(target.next_page_selector))
            href = links[0].get("href") if links else None
            if not href:
                metadata["stop_reason"] = "no_next_page"
                break
            proposed = urljoin(next_url, href)
            _require_listing_host(proposed, target)
            if proposed == next_url:
                metadata["repeated_page"] = True
                metadata["stop_reason"] = "repeated_page"
                break
            next_url = proposed
        if page_index + 1 < maximum_pages:
            time.sleep(RETRY_DELAY_SECONDS)
    else:
        metadata["stop_reason"] = "maximum_pages_reached"
    return jobs


def validate_target(target: CompanyTarget) -> None:
    missing = [name for name, value in (
        ("listing_url", target.listing_url), ("endpoint_variant", target.endpoint_variant),
        ("listing_selector", target.listing_selector), ("job_card_selector", target.job_card_selector),
        ("title_selector", target.title_selector), ("detail_link_selector", target.detail_link_selector),
        ("allowed_apply_hosts", target.allowed_apply_hosts),
    ) if not value]
    if missing:
        raise AdapterError(f"Static HTML target is missing {', '.join(missing)}")
    if target.endpoint_variant not in SUPPORTED_VARIANTS:
        raise AdapterError(f"Static HTML unsupported variant: {target.endpoint_variant}")
    _require_https(str(target.listing_url), "listing_url")
    if target.pagination_url_template:
        if str(target.pagination_url_template).count("{page}") != 1:
            raise AdapterError("Static HTML pagination_url_template must contain one {page} placeholder")
        _require_https(str(target.pagination_url_template).replace("{page}", "1"), "pagination_url_template")
    for value in (target.listing_selector, target.job_card_selector, target.title_selector,
                  target.location_selector, target.detail_link_selector, target.date_selector,
                  target.department_selector, target.next_page_selector, target.empty_selector):
        if value:
            selector_to_xpath(str(value))
    if target.external_id_attribute and not _ATTR_RE.fullmatch(str(target.external_id_attribute)):
        raise AdapterError("Static HTML external_id_attribute is invalid")
    if target.reference_url_attribute and not _ATTR_RE.fullmatch(str(target.reference_url_attribute)):
        raise AdapterError("Static HTML reference_url_attribute is invalid")
    if target.external_id_regex:
        _safe_regex(str(target.external_id_regex))
    if not target.external_id_attribute and not target.external_id_regex and not target.allow_url_id_fallback:
        raise AdapterError("Static HTML target requires an identifier extraction contract")
    _validate_hosts(target.allowed_apply_hosts)
    if not 1 <= int(target.max_pages) <= MAX_PAGES or not 1 <= int(target.max_jobs) <= MAX_RESULTS:
        raise AdapterError("Static HTML page or result limit exceeds the hard maximum")
    if not 1 <= int(target.max_response_bytes) <= MAX_RESPONSE_BYTES:
        raise AdapterError("Static HTML response byte limit exceeds the hard maximum")


def select(node: Any, selector: str) -> list[Any]:
    if selector == ":self":
        return [node]
    return list(node.xpath(selector_to_xpath(selector)))


def selector_to_xpath(selector: str) -> str:
    """Compile a deliberately small, non-executable CSS selector subset."""
    text = " ".join(str(selector).split())
    if text == ":self":
        return "."
    selector_groups = [item.strip() for item in text.split(",")]
    if not text or len(selector_groups) > 8 or any(not item for item in selector_groups):
        raise AdapterError("Static HTML selector uses an unsupported construct")
    compiled_groups = []
    for group in selector_groups:
        parts = group.split(" ")
        without_supported_pseudo = " ".join(
            re.sub(r":(?:first-child|last-child|nth-child\([1-9][0-9]*\))$", "", part)
            for part in parts
        )
        if any(token in without_supported_pseudo for token in (">", "(", ")", "|", "\\")):
            raise AdapterError("Static HTML selector uses an unsupported construct")
        compiled_groups.append(".//" + "//".join(_simple_selector(part) for part in parts))
    return " | ".join(compiled_groups)


def _simple_selector(part: str) -> str:
    pseudo = None
    pseudo_match = re.search(r":(first-child|last-child|nth-child\(([1-9][0-9]*)\))$", part)
    if pseudo_match:
        pseudo = pseudo_match.group(1)
        part = part[:pseudo_match.start()]
    tag_match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*|\*)", part)
    tag = tag_match.group(1) if tag_match else "*"
    rest = part[tag_match.end():] if tag_match else part
    predicates: list[str] = []
    while rest:
        if rest.startswith(".") or rest.startswith("#"):
            marker, rest = rest[0], rest[1:]
            match = re.match(r"[A-Za-z0-9_-]+", rest)
            if not match:
                raise AdapterError("Static HTML selector token is invalid")
            value, rest = match.group(0), rest[match.end():]
            if marker == ".":
                predicates.append(f"contains(concat(' ', normalize-space(@class), ' '), ' {value} ')")
            else:
                predicates.append(f"@id={_xpath_literal(value)}")
        elif rest.startswith("["):
            end = rest.find("]")
            if end < 0:
                raise AdapterError("Static HTML selector attribute is unterminated")
            expression, rest = rest[1:end], rest[end + 1:]
            match = _ATTR_RE.fullmatch(expression)
            if not match:
                raise AdapterError("Static HTML selector attribute is invalid")
            name, operator, value = match.groups()
            if operator is None:
                predicates.append(f"@{name}")
            elif operator == "=":
                predicates.append(f"@{name}={_xpath_literal(value)}")
            elif operator == "*=":
                predicates.append(f"contains(@{name}, {_xpath_literal(value)})")
            elif operator == "^=":
                predicates.append(f"starts-with(@{name}, {_xpath_literal(value)})")
            else:
                predicates.append(f"substring(@{name}, string-length(@{name}) - {len(value) - 1})={_xpath_literal(value)}")
        else:
            raise AdapterError("Static HTML selector token is invalid")
    if pseudo == "first-child":
        predicates.append("not(preceding-sibling::*)")
    elif pseudo == "last-child":
        predicates.append("not(following-sibling::*)")
    elif pseudo:
        predicates.append(f"count(preceding-sibling::*)={int(pseudo_match.group(2)) - 1}")
    return tag + "".join(f"[{item}]" for item in predicates)


def _xpath_literal(value: str) -> str:
    if "'" in value or '"' in value:
        raise AdapterError("Static HTML selector values cannot contain quotes")
    return f"'{value}'"


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
            raise AdapterError(f"Static HTML authentication required (HTTP {status})")
        if status in {403, 429, 451}:
            raise AdapterError(f"Static HTML blocked response (HTTP {status})")
        if status != 200:
            raise AdapterError(f"Static HTML HTTP {status}")
        return response
    raise AdapterError("Static HTML request attempts exhausted")


def _response_bytes(response: Any, target: CompanyTarget) -> bytes:
    content = getattr(response, "content", None)
    if content is None:
        content = str(getattr(response, "text", "") or "").encode("utf-8")
    if not content:
        raise AdapterError("Static HTML returned an empty HTTP 200 response")
    if len(content) > int(target.max_response_bytes):
        raise AdapterError("Static HTML response exceeded byte limit")
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if content_type and "html" not in content_type.casefold():
        raise AdapterError("Static HTML returned a non-HTML response")
    return bytes(content)


def _parse_html(content: bytes, response: Any) -> Any:
    try:
        return html.fromstring(content, base_url=getattr(response, "url", None))
    except (ValueError, TypeError) as exc:
        raise AdapterError(f"Static HTML malformed HTTP 200 response: {exc}") from exc


def _reject_non_listing(root: Any, content: bytes, valid_empty: bool) -> None:
    if valid_empty:
        return
    text = " ".join(root.text_content().split()).casefold()
    title = " ".join(root.xpath("string(//title)").split()).casefold()
    if any(token in text for token in ("cloudflare challenge", "access denied", "request blocked", "captcha")):
        raise AdapterError("Static HTML blocked response")
    if "consent required" in text or "cookie consent required" in text:
        raise AdapterError("Static HTML consent page is unsupported")
    if "enable javascript to continue" in text or "javascript is required" in text:
        raise AdapterError("Static HTML browser required")
    if re.search(r"\b(session required|session expired)\b", title):
        raise AdapterError("Static HTML session required")
    if re.search(r"\b(sign in|log in|login)\b", title):
        raise AdapterError("Static HTML authentication required")
    raise AdapterError("Static HTML malformed HTTP 200 response: listing contract not found")


def _normalize(card: Any, target: CompanyTarget, page_url: str) -> JobRecord:
    title_nodes = select(card, str(target.title_selector))
    link_nodes = select(card, str(target.detail_link_selector))
    title = _text(title_nodes[0]) if title_nodes else ""
    href = _link_reference(link_nodes[0], target) if link_nodes else ""
    url = _official_url(urljoin(page_url, str(href or "")), target)
    external_id = ""
    if target.external_id_attribute:
        external_id = str(card.get(str(target.external_id_attribute)) or "").strip()
    if not external_id and target.external_id_regex:
        match = _safe_regex(str(target.external_id_regex)).search(url)
        external_id = (match.group(1) if match and match.groups() else match.group(0) if match else "")
    if not external_id and target.allow_url_id_fallback:
        canonical = urlunparse(urlparse(url)._replace(query="", fragment=""))
        external_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    if not title or not external_id:
        raise AdapterError("Static HTML job is missing title or stable identifier")
    locations = _texts(card, target.location_selector)
    departments = _texts(card, target.department_selector)
    dates = _texts(card, target.date_selector)
    work_modes = [value for value in locations if re.search(r"(?i)\b(remote|hybrid)\b", value)]
    host = (urlparse(str(target.listing_url)).hostname or "").casefold()
    return JobRecord(
        job_id=f"static_html:{host}:{external_id}", title=title, company=target.name,
        location=" | ".join(locations), url=url, updated_at=dates[0] if dates else "",
        source=f"static_html:{host}", departments=departments, offices=locations,
        raw_metadata={"platform": "static_html", "endpoint_variant": target.endpoint_variant,
                      "external_job_id": external_id, "work_modes": work_modes},
    )


def _texts(card: Any, selector: Optional[str]) -> list[str]:
    if not selector:
        return []
    return _unique(_text(node) for node in select(card, selector))


def _text(node: Any) -> str:
    return " ".join(node.text_content().split())


def _card_identity(card: Any, target: CompanyTarget) -> str:
    links = select(card, str(target.detail_link_selector))
    reference = _link_reference(links[0], target) if links else ""
    if reference:
        return reference
    if target.external_id_attribute and card.get(str(target.external_id_attribute)):
        return str(card.get(str(target.external_id_attribute)))
    return _text(card)[:200]


def _official_url(url: str, target: CompanyTarget) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    listing_host = (urlparse(str(target.listing_url)).hostname or "").casefold()
    if parsed.scheme == "http" and host == listing_host and _allowed_host(host, target.allowed_apply_hosts):
        parsed = parsed._replace(scheme="https")
    if parsed.scheme != "https" or not _allowed_host(host, target.allowed_apply_hosts):
        raise AdapterError("Static HTML job has no official allowed-host apply URL")
    return urlunparse(parsed._replace(fragment=""))


def _link_reference(node: Any, target: CompanyTarget) -> str:
    attribute = str(target.reference_url_attribute or "href")
    return str(node.get(attribute) or "").strip()


def _require_listing_host(url: str, target: CompanyTarget) -> None:
    expected = (urlparse(str(target.listing_url)).hostname or "").casefold()
    actual = (urlparse(url).hostname or "").casefold()
    if actual != expected:
        raise AdapterError("Static HTML pagination left the reviewed listing host")


def _require_https(url: str, field: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise AdapterError(f"Static HTML {field} must be an anonymous HTTPS URL")
    if any(token in parsed.path.casefold() for token in ("/login", "/signin", "/session")):
        raise AdapterError(f"Static HTML {field} cannot require authentication or session state")


def _safe_regex(value: str) -> re.Pattern[str]:
    if len(value) > 240 or any(token in value for token in ("(?=", "(?<", "(?!", "(?P", "\\1", "\\g")):
        raise AdapterError("Static HTML identifier regex uses an unsafe construct")
    try:
        return re.compile(value)
    except re.error as exc:
        raise AdapterError(f"Static HTML identifier regex is invalid: {exc}") from exc


def _validate_hosts(hosts: Iterable[str]) -> None:
    for host in hosts:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", str(host)):
            raise AdapterError("Static HTML allowed_apply_hosts contains an invalid host")


def _allowed_host(host: str, allowed: Iterable[str]) -> bool:
    return any(host == value.casefold() or host.endswith("." + value.casefold()) for value in allowed)


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split()).strip(" |")
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            result.append(text)
    return result
