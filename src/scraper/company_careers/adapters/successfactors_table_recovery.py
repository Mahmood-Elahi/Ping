"""Exact opt-in table repairs reused from the retained run368 implementation.

Unreviewed sources retain the existing parser. Profiles bind the complete
production-preprocessed target except operational limits; limits never prove
completion. No new locale/navigation exception is granted here.
"""
from __future__ import annotations
from dataclasses import asdict
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Optional
from html import unescape
from urllib.parse import parse_qsl, urljoin, urlparse
from src.scraper.company_careers.adapters.sap_successfactors import (
    _Node, _ParsedPage, _walk, _node_text, _clean_text, _first_descendant,
    PAGE_SIZE,
)
from src.scraper.company_careers.models import AdapterError

PROFILE_PATH = Path(__file__).with_name('successfactors_stage2_profiles.json')
OPERATIONAL = frozenset({'max_pages', 'max_jobs', 'max_response_bytes', 'source_activation'})


def contract_hash(target):
    value = {k: v for k, v in asdict(target).items() if k not in OPERATIONAL}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@lru_cache(maxsize=1)
def profiles():
    return json.loads(PROFILE_PATH.read_text())


def is_reviewed(target):
    if target.platform != 'sap_successfactors':
        return False
    sealed = profiles()
    source_host = urlparse(str(target.search_url or '')).hostname
    for employer, binding in sealed.items():
        if source_host == urlparse(binding['search_url']).hostname and target.name != employer:
            raise AdapterError('SAP SuccessFactors Stage 2 employer identity did not match reviewed host')
    profile = sealed.get(target.name)
    if profile is None:
        return False
    actual = contract_hash(target)
    if actual == profile['reviewed']:
        return True
    # The normal scanner consumes effective loader targets directly; student
    # scans additionally materialize their existing enrichment flag. Preserve
    # both exact baseline forms without allowing candidate profile drift.
    if actual in {profile['baseline'], profile['baseline_effective']}:
        return False
    raise AdapterError('SAP SuccessFactors Stage 2 source profile identity did not match its exact contract')


def contract_error(message, **_diagnostic):
    # Stage 1 owns bounded runtime failure diagnostics; no new text projection.
    return AdapterError(message)


def _parse_table_page(
    root: _Node,
    search_url: str,
    offset: int,
    expected_page_size: Optional[int],
) -> _ParsedPage:
    tables = [node for node in _walk(root) if node.attrs.get("id", "").casefold() == "searchresults"]
    explicit_zero = bool(re.search(r"\b0\s+most\s+recent\s+jobs\b", _node_text(root), re.I))
    if not tables:
        if explicit_zero:
            result_range = _table_result_range(root)
            if result_range != (0, 0, 0) or any("jobtitle-link" in n.classes for n in _walk(root)):
                raise AdapterError("SAP SuccessFactors zero-job response has contradictory result metadata")
            next_request = _table_next_request(root, search_url, offset, PAGE_SIZE, 0, 0)
            return _ParsedPage([], 0, PAGE_SIZE, next_request)
        raise AdapterError("SAP SuccessFactors table response is missing searchresults")

    if len(tables) != 1:
        raise AdapterError("SAP SuccessFactors table response has duplicate searchresults")
    table = tables[0]
    header_rows = [n for n in _walk(table) if n.tag == "tr" and any(c.tag == "th" for c in n.children)]
    if len(header_rows) != 1:
        raise AdapterError("SAP SuccessFactors table response has malformed header rows")
    header_cells = [c for c in header_rows[0].children if c.tag in {"td", "th"}]
    has_layout = bool(header_cells and _empty_table_layout_cell(header_cells[-1]))
    if any(c.tag != "th" for c in (header_cells[:-1] if has_layout else header_cells)):
        raise AdapterError("SAP SuccessFactors table response has malformed layout header")
    header_nodes = [node for node in _walk(table) if node.tag == "th"]
    header_ids = [node.attrs.get("id", "").casefold() for node in header_nodes]
    header_labels = [_clean_text(_node_text(node)) for node in header_nodes]
    if (
        not header_nodes
        or any(not value for value in header_ids)
        or len(header_ids) != len(set(header_ids))
        or any(not value for value in header_labels)
        or len({value.casefold() for value in header_labels}) != len(header_labels)
    ):
        raise AdapterError(
            "SAP SuccessFactors table response has malformed or duplicate column headers"
        )
    headers = dict(zip(header_ids, header_labels))
    row_nodes = [node for node in _walk(table) if "data-row" in node.classes]
    items = [_table_item(row, headers, has_layout=has_layout) for row in row_nodes]
    items = [item for item in items if item]
    result_range = _table_result_range(root)
    if result_range is None:
        raise AdapterError("SAP SuccessFactors table response is missing total-result metadata")
    result_start, result_end, total = result_range
    expected_start = 0 if total == 0 else offset + 1
    if (
        result_start != expected_start
        or result_end != offset + len(items)
        or result_end > total
        or (total > offset and not items)
    ):
        raise AdapterError("SAP SuccessFactors table response has impossible result metadata")
    page_size = expected_page_size or (
        PAGE_SIZE
        if total == 0
        else (len(items) if total <= len(items) else result_end - offset)
    )
    if page_size < 1 or page_size > PAGE_SIZE:
        raise AdapterError("SAP SuccessFactors table response has an unsupported page size")
    if total == 0 and items:
        raise AdapterError("SAP SuccessFactors zero-job response unexpectedly contained jobs")
    if len(items) > page_size:
        raise AdapterError("SAP SuccessFactors table page exceeded the observed page size")

    next_request = _table_next_request(root, search_url, offset, page_size, result_end, total)
    return _ParsedPage(items, total, page_size, next_request)


def _table_next_request(root: _Node, search_url: str, offset: int, page_size: int,
                        result_end: int, total: int) -> Optional[tuple[str, None]]:
    next_request = None
    wanted_offset = offset + page_size
    for node in _walk(root):
        if node.tag != "a":
            continue
        explicit_next = ("next" in node.attrs.get("rel", "").casefold().split()
                         or _node_text(node).casefold() in {"next", "next page"}
                         or node.attrs.get("aria-label", "").casefold() in {"next", "next page"})
        if not node.attrs.get("href"):
            if explicit_next and node.attrs.get("aria-disabled") != "true":
                raise AdapterError("SAP SuccessFactors pagination has an unreviewed next-page continuation")
            continue
        candidate = urljoin(search_url, unescape(node.attrs["href"]))
        pairs = parse_qsl(urlparse(candidate).query, keep_blank_values=True)
        values = dict(pairs)
        if "startrow" not in values:
            if explicit_next:
                raise AdapterError("SAP SuccessFactors pagination has an unreviewed next-page continuation")
            continue
        if len(pairs) != len(values) or not re.fullmatch(r"0|[1-9][0-9]*", values["startrow"]):
            raise AdapterError("SAP SuccessFactors pagination has malformed or duplicate startrow")
        _validate_table_destination(candidate, search_url, int(values["startrow"]), diagnostic_stage="pagination_anchor")
        if explicit_next and int(values['startrow']) != wanted_offset:
            raise AdapterError('SAP SuccessFactors pagination has contradictory next offset')
        if result_end >= total and (int(values["startrow"]) > offset
                                    or explicit_next):
            raise AdapterError("SAP SuccessFactors terminal total contradicts next-page continuation")
        if values.get("startrow") == str(wanted_offset):
            _validate_table_destination(candidate, search_url, wanted_offset, diagnostic_stage="pagination_anchor")
            if next_request is not None and next_request[0] != candidate:
                raise AdapterError("SAP SuccessFactors pagination has conflicting next destinations")
            next_request = (candidate, None)
    return next_request


def _empty_table_layout_cell(cell: _Node) -> bool:
    """Only the observed trailing, empty hidden-phone TD is a layout cell."""
    return (cell.tag == "td" and cell.attrs == {"class": "hidden-phone"}
            and not cell.children and not _node_text(cell))


def _validate_table_destination(url: str, search_url: str, offset: int, *, diagnostic_stage: str = "destination_validation") -> None:
    search, destination = urlparse(search_url), urlparse(url)
    pairs = parse_qsl(destination.query, keep_blank_values=True)
    values = dict(pairs)
    if (destination.scheme != "https" or destination.hostname != search.hostname
            or destination.netloc != search.netloc or destination.path != search.path
            or destination.fragment or destination.username or destination.password
            or len(pairs) != len(values)
            or set(values) - {"startrow", "q", "locationsearch", "sortColumn", "sortDirection"}
            or values.get("q", "") or values.get("locationsearch", "")
            or values.get("startrow", str(offset)) != str(offset)
            or (offset and "startrow" not in values)):
        raise contract_error("SAP SuccessFactors table destination identity did not match reviewed search_url",
                             code="sf_table_destination_rejected",stage=diagnostic_stage,
                             url=url,reviewed_url=search_url,offset=offset)


def _table_item(row: _Node, headers: Mapping[str, str], *, has_layout: bool = False) -> Optional[dict[str, Any]]:
    link = _first_descendant(row, lambda node: node.tag == "a" and "jobtitle-link" in node.classes)
    if link is None:
        raise AdapterError("SAP SuccessFactors table row is missing its title link")
    cells = [cell for cell in row.children if cell.tag == "td"]
    if has_layout:
        if len(cells) != len(headers) + 1 or not _empty_table_layout_cell(cells[-1]):
            raise AdapterError("SAP SuccessFactors table row length does not match its schema: malformed layout cell")
        cells = cells[:-1]
    links = [n for n in _walk(row) if n.tag == "a" and "jobtitle-link" in n.classes]
    if any(n.attrs.get("href", "") != link.attrs.get("href", "") for n in links):
        raise contract_error("SAP SuccessFactors repeated stable title links did not match",
                             code="sf_title_link_disagreement",stage="record_identity")
    if len(cells) != len(headers):
        raise AdapterError("SAP SuccessFactors table row length does not match its schema")
    fields: dict[str, list[str]] = {}
    seen_headers: set[str] = set()
    for cell in cells:
        references = cell.attrs.get("headers", "").split()
        if len(references) != 1:
            raise AdapterError(
                "SAP SuccessFactors table cell has malformed column attribution"
            )
        header_id = references[0].casefold()
        label = headers.get(header_id, "")
        if not label or header_id in seen_headers:
            raise AdapterError(
                "SAP SuccessFactors table row has unknown or duplicate column attribution"
            )
        seen_headers.add(header_id)
        value = _node_text(cell)
        if label and value:
            fields.setdefault(label, []).append(value)
    return {"title": _node_text(link), "href": link.attrs.get("href", ""), "fields": fields}


def _table_result_range(root: _Node) -> Optional[tuple[int, int, int]]:
    ranges = []
    for node in _walk(root):
        if "paginationlabel" not in node.classes:
            continue
        match = re.search(
            r"Results\s+([0-9][0-9,]*)\s*[–-]\s*([0-9][0-9,]*)\s+of\s+([0-9][0-9,]*)",
            _node_text(node), re.I,
        )
        if not match:
            raise AdapterError("SAP SuccessFactors table response has malformed total-result metadata")
        ranges.append(tuple(int(value.replace(",", "")) for value in match.groups()))
    if len(set(ranges)) > 1:
        raise AdapterError("SAP SuccessFactors table response has contradictory total-result metadata")
    explicit_zero = bool(re.search(r"\b0\s+most\s+recent\s+jobs\b", _node_text(root), re.I))
    if ranges:
        if explicit_zero and ranges[0] != (0, 0, 0):
            raise AdapterError("SAP SuccessFactors table response has contradictory zero-result metadata")
        return ranges[0]
    return (0, 0, 0) if explicit_zero else None
