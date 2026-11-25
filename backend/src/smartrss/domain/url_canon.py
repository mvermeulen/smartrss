"""Pure URL canonicalization, content hashing, and fallback GUID helpers."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAM_PREFIXES: tuple[str, ...] = ("utm_", "mtm_", "pk_")
TRACKING_PARAM_NAMES: frozenset[str] = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "ref_url",
        "source",
        "igshid",
        "si",
        "spm",
        "yclid",
        "_hsenc",
        "_hsmi",
        "mkt_tok",
        "oly_anon_id",
        "oly_enc_id",
        "vero_id",
        "s_cid",
    }
)

_WHITESPACE_RE = re.compile(r"\s+")


def _is_tracking_param(key: str) -> bool:
    lowered = key.lower()
    if lowered in TRACKING_PARAM_NAMES:
        return True
    return any(lowered.startswith(prefix) for prefix in TRACKING_PARAM_PREFIXES)


def canonicalize_url(raw_url: str) -> str:
    """Return a canonical HTTP(S) URL with fragments and tracking params stripped."""
    stripped = raw_url.strip()
    parts = urlsplit(stripped)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"Invalid HTTP(S) URL: {raw_url!r}")

    host = parts.hostname.lower()
    host_fmt = f"[{host}]" if ":" in host else host

    port = parts.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    netloc = f"{host_fmt}:{port}" if port is not None else host_fmt

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    query_items = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(k)
    ]
    query_items.sort(key=lambda pair: (pair[0], pair[1]))
    query = urlencode(query_items, doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_text(text: str) -> str:
    """Collapse whitespace and trim for deterministic content hashing."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def compute_text_hash(title: str, body: str | None) -> str:
    """Compute SHA-256 of normalized 'title\\nbody'."""
    norm_title = normalize_text(title)
    norm_body = normalize_text(body or "")
    payload = f"{norm_title}\n{norm_body}".encode()
    return hashlib.sha256(payload).hexdigest()


def derive_fallback_guid(raw_url: str, title: str) -> str:
    """Derive a stable fallback GUID when a feed entry omits <guid>/<id>.

    Uses raw_url + trimmed title (not canonical_url) so future url_canon changes
    never re-ingest id-less entries and same-link entries with distinct titles
    do not collide.
    """
    digest = hashlib.sha256(f"{raw_url}\n{title.strip()}".encode()).hexdigest()
    return f"fallback:{digest}"


def default_title_from_url(feed_url: str) -> str:
    """Derive a readable placeholder title from a feed URL hostname/path."""
    parts = urlsplit(feed_url.strip())
    host = (parts.hostname or feed_url).lower()
    path = parts.path.rstrip("/")
    if path and path != "/":
        return f"{host}{path}"
    return host
