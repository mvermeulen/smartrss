#!/usr/bin/env python3
"""Zero-dependency localhost static HTTP server for SmartRSS Phase 1 client (1c §4.1).

Binds strictly to 127.0.0.1:<port> (default 4180), enforces a strict Host allow-list,
canonicalizes 127.0.0.1 / [::1] requests to http://localhost:<port> via 308, validates
SMARTRSS_API_ORIGIN to prevent CSP header injection, pins CSP connect-src, prevents
path traversal and symlink escapes, and injects a deterministic origin tag into /sw.js
so API origin changes trigger a Service Worker refresh.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import signal
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

DEFAULT_PORT = 4180
DEFAULT_API_ORIGIN = "http://localhost:8080"

SAFE_MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}

FORBIDDEN_ORIGIN_CHARS = frozenset(" \t\r\n;,")


def validate_api_origin(raw: str) -> str:
    """Validate and normalize SMARTRSS_API_ORIGIN (http/https origin only, no CSP injection)."""
    if not raw or not isinstance(raw, str):
        raise ValueError("SMARTRSS_API_ORIGIN must be a non-empty http(s) origin")
    trimmed = raw.strip()
    if not trimmed:
        raise ValueError("SMARTRSS_API_ORIGIN must be a non-empty http(s) origin")
    for ch in trimmed:
        if ord(ch) < 0x20 or ord(ch) == 0x7F or ch in FORBIDDEN_ORIGIN_CHARS:
            raise ValueError(f"SMARTRSS_API_ORIGIN contains forbidden character: {ch!r}")

    parts = urlsplit(trimmed)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"SMARTRSS_API_ORIGIN must use http or https scheme, got {scheme!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError("SMARTRSS_API_ORIGIN must not contain credentials")
    if not parts.hostname:
        raise ValueError("SMARTRSS_API_ORIGIN must include a hostname")
    if parts.path not in ("", "/"):
        raise ValueError("SMARTRSS_API_ORIGIN must not include a path")
    if parts.query or parts.fragment:
        raise ValueError("SMARTRSS_API_ORIGIN must not include query or fragment")

    # Access parts.port to ensure port is valid integer in 1..65535 if present
    port = parts.port
    host = parts.hostname.lower()
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        if port < 1 or port > 65535:
            raise ValueError(f"Invalid port in SMARTRSS_API_ORIGIN: {port}")
        netloc = f"{netloc}:{port}"
    return f"{scheme}://{netloc}"


def validate_port(raw: int | str) -> int:
    """Validate client runner port (unprivileged 1024..65535)."""
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid port: {raw!r}") from exc
    if port < 1024 or port > 65535:
        raise ValueError(f"SMARTRSS_CLIENT_PORT must be between 1024 and 65535, got {port}")
    return port


def compute_origin_tag(api_origin: str) -> str:
    """Deterministic 12-hex tag appended to /sw.js so origin changes force SW reinstall."""
    return hashlib.sha256(api_origin.encode("utf-8")).hexdigest()[:12]


def build_csp_header(api_origin: str) -> str:
    """Build strict SPA Content-Security-Policy pinning connect-src to api_origin."""
    return (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self'; "
        "font-src 'self'; "
        f"connect-src 'self' {api_origin}; "
        "manifest-src 'self'; "
        "worker-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none';"
    )


def sanitize_redirect_target(raw_path: str) -> str:
    """Normalize path+query for canonical 308 redirect, blocking protocol-relative redirects."""
    split = urlsplit(raw_path)
    path = split.path or "/"
    while path.startswith("//"):
        path = path[1:]
    if not path.startswith("/"):
        path = "/" + path
    return f"{path}?{split.query}" if split.query else path


def resolve_static_path(dist_root: Path, raw_url_path: str) -> tuple[Path | None, int]:
    """Safely resolve a URL path under dist_root.

    Returns (resolved_file, http_status):
      - (file_path, 200) for an existing regular file or SPA index.html fallback
      - (None, 403) for traversal / forbidden syntax
      - (None, 404) for missing asset with an explicit file extension (e.g. .js/.css)
    """
    # Strip query/fragment if passed raw
    path_only = urlsplit(raw_url_path).path
    if "\x00" in path_only or "\\" in path_only:
        return None, HTTPStatus.FORBIDDEN

    decoded_once = unquote(path_only)
    if "\x00" in decoded_once or "\\" in decoded_once:
        return None, HTTPStatus.FORBIDDEN

    # Also check a second decode layer for double-encoded traversal (%252e%252e)
    decoded_twice = unquote(decoded_once)
    if "\x00" in decoded_twice or "\\" in decoded_twice:
        return None, HTTPStatus.FORBIDDEN

    for candidate_str in (path_only, decoded_once, decoded_twice):
        segments = candidate_str.split("/")
        if any(seg in ("..", ".") for seg in segments if seg != ""):
            return None, HTTPStatus.FORBIDDEN

    rel = decoded_once.lstrip("/")
    if not rel:
        index_path = (dist_root / "index.html").resolve()
        if index_path.is_file() and index_path.is_relative_to(dist_root):
            return index_path, HTTPStatus.OK
        return None, HTTPStatus.NOT_FOUND

    normalized_rel = posixpath.normpath("/" + rel).lstrip("/")
    if normalized_rel.startswith(".."):
        return None, HTTPStatus.FORBIDDEN

    candidate = dist_root / normalized_rel
    try:
        resolved = candidate.resolve()
    except OSError:
        return None, HTTPStatus.FORBIDDEN

    if not resolved.is_relative_to(dist_root):
        return None, HTTPStatus.FORBIDDEN

    if resolved.is_file():
        return resolved, HTTPStatus.OK

    # Extension-aware SPA fallback: missing assets with extensions return 404, not HTML
    last_segment = normalized_rel.rsplit("/", 1)[-1]
    if "." in last_segment and not last_segment.startswith("."):
        return None, HTTPStatus.NOT_FOUND

    index_path = (dist_root / "index.html").resolve()
    if index_path.is_file() and index_path.is_relative_to(dist_root):
        return index_path, HTTPStatus.OK
    return None, HTTPStatus.NOT_FOUND


def cache_control_for_path(url_path: str, resolved_file: Path, dist_root: Path) -> str:
    """Return Cache-Control value per 1c §4.1 cache matrix."""
    rel_posix = resolved_file.relative_to(dist_root).as_posix()
    if rel_posix.startswith("assets/"):
        return "public, max-age=31536000, immutable"
    if rel_posix in ("index.html", "sw.js", "manifest.webmanifest") or url_path in (
        "/",
        "/index.html",
        "/sw.js",
        "/manifest.webmanifest",
    ):
        return "no-cache"
    return "no-cache"


class SmartRSSClientServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying validated SmartRSS runner configuration."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        dist_dir: Path,
        api_origin: str,
        canonical_port: int | None = None,
    ) -> None:
        self.dist_root = dist_dir.resolve()
        if not self.dist_root.is_dir():
            raise ValueError(f"SMARTRSS_DIST_DIR does not exist or is not a directory: {dist_dir}")
        self.api_origin = validate_api_origin(api_origin)
        self.origin_tag = compute_origin_tag(self.api_origin)
        self.csp_header = build_csp_header(self.api_origin)
        self._configured_port = canonical_port
        super().__init__(server_address, SmartRSSClientHandler)

    @property
    def effective_port(self) -> int:
        if self._configured_port is not None:
            return self._configured_port
        return int(self.server_address[1])

    @property
    def canonical_host(self) -> str:
        return f"localhost:{self.effective_port}"

    @property
    def redirect_hosts(self) -> frozenset[str]:
        port = self.effective_port
        return frozenset({f"127.0.0.1:{port}", f"[::1]:{port}"})

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset({self.canonical_host, *self.redirect_hosts})


class SmartRSSClientHandler(BaseHTTPRequestHandler):
    """Hardened static SPA handler with Host check, 308 canonicalization, and strict CSP."""

    server: SmartRSSClientServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Keep logs clean and never log request headers/tokens
        sys.stderr.write(f"[smartrss-client] {self.command} {urlsplit(self.path).path}\n")

    def _send_security_headers(self, *, cache_control: str) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
        )
        self.send_header("Content-Security-Policy", self.server.csp_header)
        self.send_header("Cache-Control", cache_control)

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        *,
        content_type: str,
        cache_control: str,
        extra_headers: dict[str, str] | None = None,
        head_only: bool = False,
    ) -> None:
        self.send_response(status)
        self._send_security_headers(cache_control=cache_control)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        if not head_only and body:
            self.wfile.write(body)

    def _check_host(self, *, head_only: bool) -> bool:
        raw_host = (self.headers.get("Host") or "").strip().lower()
        if raw_host not in self.server.allowed_hosts:
            self._send_bytes(
                HTTPStatus.BAD_REQUEST,
                b"Invalid Host header\n",
                content_type="text/plain; charset=utf-8",
                cache_control="no-store",
                head_only=head_only,
            )
            return False
        if raw_host in self.server.redirect_hosts:
            target = sanitize_redirect_target(self.path)
            location = f"http://{self.server.canonical_host}{target}"
            self._send_bytes(
                HTTPStatus.PERMANENT_REDIRECT,
                b"",
                content_type="text/plain; charset=utf-8",
                cache_control="no-store",
                extra_headers={"Location": location},
                head_only=head_only,
            )
            return False
        return True

    def _handle_read(self, *, head_only: bool) -> None:
        if not self._check_host(head_only=head_only):
            return

        clean_path = urlsplit(self.path).path

        if clean_path == "/client-config.json":
            payload = json.dumps(
                {
                    "mode": "localhost",
                    "api_origin": self.server.api_origin,
                }
            ).encode("utf-8")
            self._send_bytes(
                HTTPStatus.OK,
                payload,
                content_type="application/json; charset=utf-8",
                cache_control="no-store",
                head_only=head_only,
            )
            return

        resolved, status = resolve_static_path(self.server.dist_root, self.path)
        if resolved is None:
            msg = b"Forbidden\n" if status == HTTPStatus.FORBIDDEN else b"Not Found\n"
            self._send_bytes(
                status,
                msg,
                content_type="text/plain; charset=utf-8",
                cache_control="no-store",
                head_only=head_only,
            )
            return

        body = resolved.read_bytes()
        rel_posix = resolved.relative_to(self.server.dist_root).as_posix()
        if rel_posix == "sw.js":
            body = body + f"\n// smartrss-origin-tag:{self.server.origin_tag}\n".encode()

        mime = SAFE_MIME_TYPES.get(resolved.suffix.lower(), "application/octet-stream")
        cache_control = cache_control_for_path(clean_path, resolved, self.server.dist_root)
        self._send_bytes(
            HTTPStatus.OK,
            body,
            content_type=mime,
            cache_control=cache_control,
            head_only=head_only,
        )

    def _method_not_allowed(self) -> None:
        self._send_bytes(
            HTTPStatus.METHOD_NOT_ALLOWED,
            b"Method Not Allowed\n",
            content_type="text/plain; charset=utf-8",
            cache_control="no-store",
            extra_headers={"Allow": "GET, HEAD"},
        )

    def do_GET(self) -> None:  # noqa: N802
        self._handle_read(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle_read(head_only=True)

    def do_POST(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._method_not_allowed()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SmartRSS localhost static client runner")
    parser.add_argument(
        "--port",
        default=os.environ.get("SMARTRSS_CLIENT_PORT", str(DEFAULT_PORT)),
        help=f"Loopback port to listen on (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--api-origin",
        default=os.environ.get("SMARTRSS_API_ORIGIN", DEFAULT_API_ORIGIN),
        help=f"SmartRSS LAN API origin (default: {DEFAULT_API_ORIGIN})",
    )
    parser.add_argument(
        "--dist-dir",
        default=os.environ.get("SMARTRSS_DIST_DIR", str(Path(__file__).resolve().parent / "dist")),
        help="Path to compiled SmartRSS frontend dist/ directory",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        port = validate_port(args.port)
        api_origin = validate_api_origin(args.api_origin)
        dist_dir = Path(args.dist_dir)
        server = SmartRSSClientServer(
            ("127.0.0.1", port),
            dist_dir=dist_dir,
            api_origin=api_origin,
            canonical_port=port,
        )
    except ValueError as exc:
        sys.stderr.write(f"smartrss-client configuration error: {exc}\n")
        return 2

    def _shutdown_handler(_signum: int, _frame: Any) -> None:
        # Must call shutdown() from a separate thread when invoked inside signal handler
        import threading

        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    sys.stderr.write(
        f"SmartRSS client runner listening on http://localhost:{port} "
        f"(API origin: {api_origin}, dist: {server.dist_root})\n"
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
