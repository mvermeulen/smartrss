"""Pure OPML 2.0 parser (using defusedxml) and serializer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from xml.etree import ElementTree as StdET  # noqa: S405 - only used for serialization

import defusedxml.ElementTree as DefusedET
from defusedxml import DefusedXmlException

MAX_OPML_NESTING_DEPTH = 32


class OpmlParseError(ValueError):
    """Raised when OPML XML is malformed, unsafe (XXE/entity bomb), or not OPML."""


@dataclass(frozen=True, slots=True)
class OpmlSubscription:
    feed_url: str
    title: str | None = None
    site_url: str | None = None
    category_name: str | None = None


def parse_opml(opml_xml: str, *, max_depth: int = MAX_OPML_NESTING_DEPTH) -> list[OpmlSubscription]:
    """Parse OPML 2.0 XML safely with defusedxml and iterative depth-bounded traversal (I-12)."""
    if not opml_xml or not opml_xml.strip():
        raise OpmlParseError("OPML payload is empty")

    try:
        root = DefusedET.fromstring(opml_xml.encode("utf-8"))
    except (DefusedXmlException, StdET.ParseError, ValueError) as exc:
        raise OpmlParseError(f"Invalid or unsafe OPML XML: {exc}") from exc

    if root.tag.lower() != "opml":
        raise OpmlParseError(f"Expected <opml> root element, got <{root.tag}>")

    body = root.find("body")
    if body is None:
        raise OpmlParseError("OPML document is missing <body>")

    subscriptions: list[OpmlSubscription] = []
    # Iterative stack: (element, current_category, depth)
    stack: list[tuple[StdET.Element, str | None, int]] = [
        (outline, None, 1) for outline in reversed(body.findall("outline"))
    ]

    while stack:
        outline, current_category, depth = stack.pop()
        if depth > max_depth:
            raise OpmlParseError(f"OPML nesting depth exceeded maximum ({max_depth})")

        xml_url = (outline.attrib.get("xmlUrl") or outline.attrib.get("xmlurl") or "").strip()
        text_attr = (outline.attrib.get("title") or outline.attrib.get("text") or "").strip()
        html_url = (outline.attrib.get("htmlUrl") or outline.attrib.get("htmlurl") or "").strip()
        children = outline.findall("outline")

        if xml_url:
            subscriptions.append(
                OpmlSubscription(
                    feed_url=xml_url,
                    title=text_attr or None,
                    site_url=html_url or None,
                    category_name=current_category,
                )
            )
        elif children:
            folder_name = text_attr or current_category
            for child in reversed(children):
                stack.append((child, folder_name, depth + 1))

    return subscriptions


def export_opml(
    feeds_by_category: Mapping[str | None, Sequence[OpmlSubscription]],
    *,
    title: str = "SmartRSS Subscriptions",
) -> str:
    """Serialize feeds grouped by category into an OPML 2.0 XML string."""
    opml = StdET.Element("opml", {"version": "2.0"})
    head = StdET.SubElement(opml, "head")
    title_el = StdET.SubElement(head, "title")
    title_el.text = title
    body = StdET.SubElement(opml, "body")

    # Uncategorized feeds first, then sorted categories
    uncategorized = feeds_by_category.get(None, ())
    for sub in uncategorized:
        _append_feed_outline(body, sub)

    for cat_name in sorted(k for k in feeds_by_category if k is not None):
        subs = feeds_by_category[cat_name]
        if not subs:
            continue
        folder = StdET.SubElement(body, "outline", {"text": cat_name, "title": cat_name})
        for sub in subs:
            _append_feed_outline(folder, sub)

    StdET.indent(opml, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + StdET.tostring(opml, encoding="unicode")


def _append_feed_outline(parent: StdET.Element, sub: OpmlSubscription) -> None:
    label = sub.title or sub.feed_url
    attrs: dict[str, str] = {
        "type": "rss",
        "text": label,
        "title": label,
        "xmlUrl": sub.feed_url,
    }
    if sub.site_url:
        attrs["htmlUrl"] = sub.site_url
    StdET.SubElement(parent, "outline", attrs)
