"""Unit tests for FTS language allow-list, HTML stripping, and XSS-safe snippets (1b-T10)."""

from __future__ import annotations

import pytest

from smartrss.domain.fts_text import (
    ALLOWED_FTS_LANGUAGES,
    MAX_FTS_BODY_CHARS,
    SNIPPET_FRAG_DELIM,
    SNIPPET_START_SEL,
    SNIPPET_STOP_SEL,
    SnippetFragment,
    parse_headline_fragments,
    prepare_headline_input,
    sanitize_headline_source,
    strip_html_to_plain_text,
    validate_fts_language,
)


def test_validate_fts_language_allow_list() -> None:
    for lang in ALLOWED_FTS_LANGUAGES:
        assert validate_fts_language(f"  {lang.upper()}  ") == lang

    for bad in ("", "klingon", "english; DROP TABLE items;", "pg_catalog.english"):
        with pytest.raises(ValueError, match="Unsupported FTS_LANGUAGE"):
            validate_fts_language(bad)


def test_strip_html_to_plain_text_skips_scripts_and_decodes_entities() -> None:
    assert strip_html_to_plain_text(None) == ""
    assert strip_html_to_plain_text("") == ""

    raw = (
        "<div><h1>Title</h1><script>alert('xss'); var x = 1;</script>"
        "<style>.a { color: red; }</style>"
        "<p>Hello &amp; <b>world</b>&#x21;</p><p>Second</p></div>"
    )
    assert strip_html_to_plain_text(raw) == "Title Hello & world ! Second"

    # Unbalanced stray </script> never crashes or goes negative
    assert strip_html_to_plain_text("</script><p>Safe</p>") == "Safe"

    # Pre-slice cap enforced
    big = "<p>" + ("a" * 500) + "</p>"
    assert len(strip_html_to_plain_text(big, max_input_chars=20)) <= 20


def test_prepare_headline_input_and_control_char_sanitization() -> None:
    dirty = f"foo{SNIPPET_START_SEL}bar{SNIPPET_STOP_SEL}baz{SNIPPET_FRAG_DELIM}qux"
    assert sanitize_headline_source(dirty) == "foobarbazqux"

    # Prefers extracted_text, caps at 64 KB
    huge_body = "x" * (MAX_FTS_BODY_CHARS + 500)
    assert (
        len(prepare_headline_input(title="T", raw_summary="<p>S</p>", extracted_text=huge_body))
        == MAX_FTS_BODY_CHARS
    )

    # Falls back to stripped raw_summary
    assert (
        prepare_headline_input(
            title="Title",
            raw_summary=f"<p>Summary {SNIPPET_START_SEL}text</p>",
            extracted_text="   ",
        )
        == "Summary text"
    )

    # Falls back to title if summary is empty HTML
    assert (
        prepare_headline_input(
            title=f"Fallback {SNIPPET_FRAG_DELIM}Title",
            raw_summary="<script>ignored()</script>",
            extracted_text=None,
        )
        == "Fallback Title"
    )


def test_parse_headline_fragments_xss_safe_and_multi_fragment() -> None:
    assert parse_headline_fragments("") == []

    # Multi-fragment with literal ellipsis inside article text and literal <script> tag
    hl = (
        f"Intro … <script>alert(1)</script> {SNIPPET_START_SEL}pgvector{SNIPPET_STOP_SEL} index"
        f"{SNIPPET_FRAG_DELIM}"
        f"{SNIPPET_START_SEL}HNSW{SNIPPET_STOP_SEL} recall … end"
    )
    parsed = parse_headline_fragments(hl)
    assert parsed == [
        [
            SnippetFragment(text="Intro … <script>alert(1)</script> ", highlight=False),
            SnippetFragment(text="pgvector", highlight=True),
            SnippetFragment(text=" index", highlight=False),
        ],
        [
            SnippetFragment(text="HNSW", highlight=True),
            SnippetFragment(text=" recall … end", highlight=False),
        ],
    ]

    # Empty fragments between delimiters are ignored; adjacent markers handled cleanly
    hl_edge = f"{SNIPPET_FRAG_DELIM}{SNIPPET_START_SEL}{SNIPPET_STOP_SEL}plain"
    assert parse_headline_fragments(hl_edge) == [[SnippetFragment(text="plain", highlight=False)]]
