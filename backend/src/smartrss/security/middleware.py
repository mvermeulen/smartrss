"""LAN hardening ASGI middlewares: Host allow-list and JSON-only mutation guard."""

from __future__ import annotations

import json
from collections.abc import Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

MUTATION_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


async def _send_json_error(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _get_header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return bytes(value).decode("latin-1")
    return None


class HostAllowListMiddleware:
    """Rejects requests whose Host header is not in the strict allow-list (DNS rebinding defense).

    Exempts GET /api/v1/health so internal Docker Compose healthchecks work without
    adding container hostnames to ALLOWED_HOSTS.
    """

    def __init__(self, app: ASGIApp, *, allowed_hosts: Iterable[str]) -> None:
        self.app = app
        self.allowed_hosts = frozenset(h.strip().lower() for h in allowed_hosts if h.strip())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        path = scope.get("path", "")
        if method == "GET" and path == "/api/v1/health":
            await self.app(scope, receive, send)
            return

        host = _get_header(scope, b"host")
        if not host or host.strip().lower() not in self.allowed_hosts:
            await _send_json_error(send, 400, "Invalid Host header")
            return

        await self.app(scope, receive, send)


class JsonMutationGuardMiddleware:
    """Requires application/json on POST/PUT/PATCH/DELETE requests with a body or Content-Type.

    Blocks cross-origin form posts (application/x-www-form-urlencoded, multipart/form-data,
    text/plain) with 415 Unsupported Media Type.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        if method not in MUTATION_METHODS:
            await self.app(scope, receive, send)
            return

        content_type = _get_header(scope, b"content-type")
        content_length_raw = _get_header(scope, b"content-length")
        transfer_encoding = _get_header(scope, b"transfer-encoding")

        has_body = False
        if transfer_encoding and transfer_encoding.strip():
            has_body = True
        elif content_length_raw is not None:
            try:
                has_body = int(content_length_raw.strip()) > 0
            except ValueError:
                has_body = True

        if content_type is not None and content_type.strip():
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                await _send_json_error(
                    send,
                    415,
                    "Unsupported Media Type: mutation requests must use application/json",
                )
                return
        elif has_body:
            await _send_json_error(
                send,
                415,
                "Unsupported Media Type: Content-Type application/json is required",
            )
            return

        await self.app(scope, receive, send)
