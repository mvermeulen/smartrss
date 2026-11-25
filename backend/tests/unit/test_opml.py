"""Unit tests for OPML 2.0 parsing (defusedxml) and export round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from smartrss.domain.opml import OpmlParseError, OpmlSubscription, export_opml, parse_opml

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def test_opml_round_trip_nested_and_flat() -> None:
    grouped = {
        None: [
            OpmlSubscription(
                feed_url="https://blog.example.com/rss.xml",
                title="Root Blog",
                site_url="https://blog.example.com",
            )
        ],
        "Engineering": [
            OpmlSubscription(
                feed_url="https://eng.example.com/atom.xml",
                title="Eng Feed",
                site_url="https://eng.example.com",
                category_name="Engineering",
            )
        ],
    }
    xml_out = export_opml(grouped)
    parsed = parse_opml(xml_out)
    assert len(parsed) == 2
    assert parsed[0] == OpmlSubscription(
        feed_url="https://blog.example.com/rss.xml",
        title="Root Blog",
        site_url="https://blog.example.com",
        category_name=None,
    )
    assert parsed[1] == OpmlSubscription(
        feed_url="https://eng.example.com/atom.xml",
        title="Eng Feed",
        site_url="https://eng.example.com",
        category_name="Engineering",
    )


def test_opml_blocks_xxe_and_entity_expansion() -> None:
    xxe_payload = """<?xml version="1.0"?>
    <!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
    <opml version="2.0"><body><outline text="&xxe;" xmlUrl="https://a.example.com/rss"/></body></opml>
    """
    with pytest.raises(OpmlParseError):
        parse_opml(xxe_payload)

    billion_laughs = """<?xml version="1.0"?>
    <!DOCTYPE lolz [
      <!ENTITY lol "lol">
      <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
    ]>
    <opml version="2.0"><body><outline text="&lol2;" xmlUrl="https://a.example.com/rss"/></body></opml>
    """
    with pytest.raises(OpmlParseError):
        parse_opml(billion_laughs)


def test_opml_rejects_malformed_or_non_opml() -> None:
    for bad in ("", "   ", "<not-xml", "<rss version='2.0'></rss>", "<opml version='2.0'></opml>"):
        with pytest.raises(OpmlParseError):
            parse_opml(bad)


def test_opml_xxe_fixture_and_nesting_depth_cap() -> None:
    """I-12 regression: deeply nested OPML raises OpmlParseError instead of RecursionError."""
    xxe_xml = (FIXTURES_DIR / "opml_xxe.xml").read_text(encoding="utf-8")
    with pytest.raises(OpmlParseError):
        parse_opml(xxe_xml)

    depth = 40
    deep_xml = (
        '<opml version="2.0"><body>'
        + '<outline text="F">' * depth
        + '<outline text="Leaf" xmlUrl="https://leaf.example.com/rss"/>'
        + "</outline>" * depth
        + "</body></opml>"
    )
    with pytest.raises(OpmlParseError, match="depth"):
        parse_opml(deep_xml)
