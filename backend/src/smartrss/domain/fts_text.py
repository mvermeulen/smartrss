"""Pure full-text search text preparation, language validation, and XSS-safe snippets (§8)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

ALLOWED_FTS_LANGUAGES: frozenset[str] = frozenset(
    {
        "english",
        "simple",
        "danish",
        "dutch",
        "finnish",
        "french",
        "german",
        "hungarian",
        "italian",
        "norwegian",
        "portuguese",
        "romanian",
        "russian",
        "spanish",
        "swedish",
        "turkish",
    }
)

MAX_HTML_PARSE_CHARS = 256 * 1024
MAX_FTS_BODY_CHARS = 65_536

SNIPPET_START_SEL = "\x02"  # STX
SNIPPET_STOP_SEL = "\x03"  # ETX
SNIPPET_FRAG_DELIM = "\x04"  # EOT

_WHITESPACE_RE = re.compile(r"\s+")
_SENTINEL_TRANS = str.maketrans("", "", SNIPPET_START_SEL + SNIPPET_STOP_SEL + SNIPPET_FRAG_DELIM)
_SKIP_TAGS: frozenset[str] = frozenset({"script", "style"})


class _PlainTextHTMLParser(HTMLParser):
    """Linear-time, non-recursive stdlib HTML stripper skipping <script>/<style> blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in _SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0:
            self._chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
        elif self._skip_depth == 0:
            self._chunks.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def get_text(self) -> str:
        return _WHITESPACE_RE.sub(" ", "".join(self._chunks)).strip()


@dataclass(frozen=True, slots=True)
class SnippetFragment:
    """Single plain-text run within a ts_headline fragment (zero HTML surface)."""

    text: str
    highlight: bool


def validate_fts_language(value: str) -> str:
    """Validate and normalize a PostgreSQL FTS regconfig name against the allow-list."""
    cleaned = value.strip().lower()
    if cleaned not in ALLOWED_FTS_LANGUAGES:
        allowed = ", ".join(sorted(ALLOWED_FTS_LANGUAGES))
        raise ValueError(f"Unsupported FTS_LANGUAGE {value!r}; allowed values: {allowed}")
    return cleaned


def strip_html_to_plain_text(
    raw_html: str | None,
    *,
    max_input_chars: int = MAX_HTML_PARSE_CHARS,
) -> str:
    """Strip HTML tags and <script>/<style> content into collapsed plain text (S1/S5)."""
    if not raw_html:
        return ""
    bounded = raw_html[:max_input_chars]
    parser = _PlainTextHTMLParser()
    parser.feed(bounded)
    parser.close()
    return parser.get_text()


def sanitize_headline_source(text: str) -> str:
    """Remove STX/ETX/EOT control sentinels from source text before passing to ts_headline."""
    return text.translate(_SENTINEL_TRANS)


def prepare_headline_input(
    *,
    title: str,
    raw_summary: str | None,
    extracted_text: str | None,
) -> str:
    """Select and sanitize plain text for ts_headline generation (extracted > summary > title)."""
    if extracted_text and extracted_text.strip():
        return sanitize_headline_source(extracted_text[:MAX_FTS_BODY_CHARS])
    if raw_summary:
        plain_summary = strip_html_to_plain_text(raw_summary[:MAX_FTS_BODY_CHARS])
        if plain_summary:
            return sanitize_headline_source(plain_summary)
    return sanitize_headline_source(title)


def parse_headline_fragments(headline: str) -> list[list[SnippetFragment]]:
    """Parse control-char delimited ts_headline output into list[list[SnippetFragment]]."""
    if not headline:
        return []

    fragments: list[list[SnippetFragment]] = []
    for raw_fragment in headline.split(SNIPPET_FRAG_DELIM):
        runs: list[SnippetFragment] = []
        in_highlight = False
        buf: list[str] = []

        for ch in raw_fragment:
            if ch == SNIPPET_START_SEL:
                if buf:
                    runs.append(SnippetFragment(text="".join(buf), highlight=in_highlight))
                    buf = []
                in_highlight = True
            elif ch == SNIPPET_STOP_SEL:
                if buf:
                    runs.append(SnippetFragment(text="".join(buf), highlight=in_highlight))
                    buf = []
                in_highlight = False
            else:
                buf.append(ch)

        if buf:
            runs.append(SnippetFragment(text="".join(buf), highlight=in_highlight))

        if runs:
            fragments.append(runs)

    return fragments
