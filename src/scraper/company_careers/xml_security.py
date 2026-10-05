"""Small shared helpers for parsing untrusted career-feed XML safely."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from lxml import etree

from src.scraper.company_careers.models import AdapterError


def local_name(node: Any) -> str:
    """Return an element's namespace-independent local name."""
    try:
        return str(etree.QName(node).localname)
    except (TypeError, ValueError):
        return ""


def direct_children(node: Any, name: str) -> list[Any]:
    wanted = str(name).casefold()
    return [child for child in node if local_name(child).casefold() == wanted]


def first_child_text(node: Any, names: Iterable[str]) -> str:
    wanted = {str(name).casefold() for name in names}
    for child in node:
        if local_name(child).casefold() in wanted:
            value = " ".join("".join(child.itertext()).split())
            if value:
                return value
    return ""


def safe_xml_root(content: bytes) -> Any:
    """Parse a complete XML document with DTDs, entities, and network disabled."""
    lowered = bytes(content).lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise AdapterError("XML DTD and entity declarations are prohibited")
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        recover=False,
        huge_tree=False,
        remove_comments=False,
    )
    try:
        return etree.fromstring(content, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise AdapterError(f"source returned malformed XML: {exc}") from exc
