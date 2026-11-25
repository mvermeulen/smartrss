"""Unit tests for URL canonicalization, text hashing, and fallback GUID derivation."""

from __future__ import annotations

import pytest

from smartrss.domain.url_canon import (
    canonicalize_url,
    compute_text_hash,
    default_title_from_url,
    derive_fallback_guid,
)


def test_canonicalize_strips_tracking_params_fragments_and_default_ports() -> None:
    url = (
        "HTTPS://Example.COM:443/Blog/Post/?utm_source=hn&b=2&fbclid=xyz&a=1"
        "&gclid=abc&ref=nav#comments"
    )
    assert canonicalize_url(url) == "https://example.com/Blog/Post?a=1&b=2"


def test_canonicalize_preserves_non_default_ports_and_root_slash() -> None:
    assert canonicalize_url("http://example.com:80/") == "http://example.com/"
    assert canonicalize_url("http://example.com:8080") == "http://example.com:8080/"
    assert canonicalize_url("https://[2001:db8::1]:443/feed/") == "https://[2001:db8::1]/feed"


def test_canonicalize_rejects_non_http_schemes() -> None:
    for bad in ("ftp://example.com/a", "javascript:alert(1)", "file:///etc/passwd", "not-a-url"):
        with pytest.raises(ValueError):
            canonicalize_url(bad)


def test_compute_text_hash_normalizes_whitespace() -> None:
    h1 = compute_text_hash("Hello   World", "Line 1\n\tLine   2 ")
    h2 = compute_text_hash("  Hello World ", "Line 1 Line 2")
    assert h1 == h2
    assert h1 != compute_text_hash("Hello World", "Different body")


def test_derive_fallback_guid_uses_raw_url_and_title() -> None:
    g1 = derive_fallback_guid("https://comic.example.com/latest", "Issue #1")
    g2 = derive_fallback_guid("https://comic.example.com/latest", "Issue #2")
    assert g1.startswith("fallback:")
    assert g1 != g2
    assert g1 == derive_fallback_guid("https://comic.example.com/latest", "  Issue #1 ")


def test_default_title_from_url() -> None:
    assert default_title_from_url("https://Example.COM/rss.xml") == "example.com/rss.xml"
    assert default_title_from_url("https://Example.COM/") == "example.com"
