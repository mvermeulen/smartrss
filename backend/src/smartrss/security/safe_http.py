"""SSRF-hardened SafeHttpClient with IP pinning, streaming decompression caps, and rate limiting."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import socket
import ssl
import time
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
AsyncResolver = Callable[[str, int], Awaitable[Sequence[IPAddress]]]

REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})
PERMANENT_REDIRECT_STATUSES: frozenset[int] = frozenset({301, 308})

DEFAULT_BLOCKED_NETWORKS: tuple[IPNetwork, ...] = (
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("fc00::/7"),  # IPv6 ULA
    ipaddress.ip_network("198.18.0.0/15"),  # Benchmark net
    ipaddress.ip_network("192.0.2.0/24"),  # TEST-NET-1
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),  # TEST-NET-3
    ipaddress.ip_network("172.17.0.0/16"),  # Default Docker bridge
)
NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")
NAT64_LOCAL_USE_PREFIX = ipaddress.ip_network("64:ff9b:1::/48")

DEFAULT_USER_AGENT = "SmartRSS/0.1 (+https://github.com/smartrss)"
_FEED_ENTRY_DISALLOWED_CHARS = frozenset('<>"')
_MARKDOWN_PASTE_SCHEME_RE = re.compile(r"(?:\]\(|\)\()https?://", re.IGNORECASE)


def validate_feed_url_syntax(url: str) -> None:
    """Reject common copy-paste artifacts at feed entry time (E-10, E-10-F1).

    Used on POST /feeds, PATCH /feeds/{id}, and OPML import — NOT on SafeHttpClient.fetch(),
    so legitimate fetched URLs, redirects, and query parameters (?url=https://..., ?tags[]=x,
    ?x=1|2) or archive paths (/web/2024/https://...) remain accepted.
    """
    if not url or any(ch.isspace() or ch in _FEED_ENTRY_DISALLOWED_CHARS for ch in url):
        raise SSRFBlockedError("URL contains disallowed whitespace or angle-bracket/quote chars")
    parts = urlsplit(url)
    if "[" in parts.path or "]" in parts.path:
        raise SSRFBlockedError("URL path contains unescaped brackets")
    non_host = (
        f"{parts.path}"
        f"{'?' + parts.query if parts.query else ''}"
        f"{'#' + parts.fragment if parts.fragment else ''}"
    )
    if _MARKDOWN_PASTE_SCHEME_RE.search(non_host):
        raise SSRFBlockedError("URL contains a pasted Markdown link")


class SafeHttpError(Exception):
    """Base error for SafeHttpClient failures."""


class SSRFBlockedError(SafeHttpError):
    """Raised when a URL scheme, port, host, or resolved IP violates SSRFPolicy."""


class SSRFConnectionError(SafeHttpError):
    """Raised when DNS resolution, TCP connect, TLS handshake, or network I/O fails."""


class SafeHttpTimeoutError(SafeHttpError):
    """Raised when an outbound HTTP connect, read, write, or pool operation times out."""


class TooManyRedirectsError(SafeHttpError):
    """Raised when redirect chain exceeds max_redirects."""


class UnsupportedEncodingError(SafeHttpError):
    """Raised when the remote server returns an unsupported or chained Content-Encoding."""


class ResponseTooLargeError(SafeHttpError):
    """Raised when wire payload or decompressed body exceeds max_bytes."""


async def default_dns_resolver(host: str, port: int) -> list[IPAddress]:
    """Resolve A/AAAA records using the event loop's getaddrinfo."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SSRFConnectionError(f"DNS resolution failed for {host!r}: {exc}") from exc

    ips: list[IPAddress] = []
    seen: set[IPAddress] = set()
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        raw_ip = str(sockaddr[0])
        if "%" in raw_ip:
            raw_ip = raw_ip.split("%", 1)[0]
        ip = ipaddress.ip_address(raw_ip)
        if ip not in seen:
            seen.add(ip)
            ips.append(ip)
    return ips


class SSRFPolicy:
    """Fail-closed SSRF validation for URLs and resolved IP addresses."""

    def __init__(
        self,
        *,
        allowed_ports: Sequence[int] = (80, 443),
        extra_blocked_cidrs: Sequence[str] = (),
        resolver: AsyncResolver | None = None,
    ) -> None:
        self.allowed_ports = frozenset(allowed_ports)
        self.extra_blocked_networks: tuple[IPNetwork, ...] = tuple(
            ipaddress.ip_network(cidr, strict=False) for cidr in extra_blocked_cidrs if cidr.strip()
        )
        self.resolver: AsyncResolver = resolver or default_dns_resolver

    def validate_url(self, url: str) -> tuple[str, str, int]:
        """Validate URL syntax, scheme, userinfo, and port. Return (scheme, host, port)."""
        if not url or any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
            raise SSRFBlockedError("URL contains control characters or is empty")

        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise SSRFBlockedError(f"Disallowed URL scheme: {scheme!r}")
        if parts.username is not None or parts.password is not None:
            raise SSRFBlockedError("Embedded URL credentials are forbidden")
        if not parts.hostname:
            raise SSRFBlockedError("URL is missing hostname")

        try:
            port = parts.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise SSRFBlockedError(f"Invalid URL port: {exc}") from exc

        if port not in self.allowed_ports:
            raise SSRFBlockedError(f"Disallowed port {port}")

        return scheme, parts.hostname, port

    def ip_block_reason(self, ip: IPAddress) -> str | None:
        """Return a block reason string if IP is forbidden, else None."""
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.ipv4_mapped is not None:
                inner_reason = self.ip_block_reason(ip.ipv4_mapped)
                if inner_reason is not None:
                    return f"IPv4-mapped IPv6 ({inner_reason})"
            if ip.sixtofour is not None:
                inner_reason = self.ip_block_reason(ip.sixtofour)
                if inner_reason is not None:
                    return f"6to4 embedded IPv4 ({inner_reason})"
            if ip in NAT64_LOCAL_USE_PREFIX:
                return "NAT64 local-use prefix (64:ff9b:1::/48)"
            if ip in NAT64_WELL_KNOWN_PREFIX:
                # Check configured extra CIDRs against the IPv6 address itself too (I-22)
                for net in self.extra_blocked_networks:
                    if ip.version == net.version and ip in net:
                        return f"configured blocked CIDR {net}"
                embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
                inner_reason = self.ip_block_reason(embedded)
                if inner_reason is not None:
                    return f"NAT64 embedded IPv4 ({inner_reason})"
                return None

        if ip.is_loopback:
            return "loopback address"
        if ip.is_private:
            return "private address"
        if ip.is_link_local:
            return "link-local address"
        if ip.is_multicast:
            return "multicast address"
        if ip.is_reserved:
            return "reserved address"
        if ip.is_unspecified:
            return "unspecified address"

        for net in DEFAULT_BLOCKED_NETWORKS:
            if ip.version == net.version and ip in net:
                return f"blocked CIDR {net}"
        for net in self.extra_blocked_networks:
            if ip.version == net.version and ip in net:
                return f"configured blocked CIDR {net}"

        return None

    def validate_url_and_literal_ip(self, url: str) -> tuple[str, str, int]:
        """Validate URL syntax, scheme, port, and block literal private/reserved IPs without DNS.

        Used during bulk OPML import (I-10) so serial DNS never stalls an HTTP transaction;
        full DNS resolution + IP pinning runs on every fetch in SafeHttpClient.
        """
        scheme, host, port = self.validate_url(url)
        raw_host = host.strip("[]")
        try:
            literal_ip = ipaddress.ip_address(raw_host)
        except ValueError:
            pass
        else:
            reason = self.ip_block_reason(literal_ip)
            if reason is not None:
                raise SSRFBlockedError(f"Blocked literal IP {literal_ip}: {reason}")
        return scheme, host, port

    async def resolve_and_validate(self, host: str, port: int) -> list[IPAddress]:
        """Resolve host and fail-closed if ANY resolved IP is blocked."""
        if port not in self.allowed_ports:
            raise SSRFBlockedError(f"Disallowed port {port}")

        # Handle literal IP addresses without invoking DNS
        raw_host = host.strip("[]")
        try:
            literal_ip = ipaddress.ip_address(raw_host)
        except ValueError:
            ips = list(await self.resolver(host, port))
        else:
            ips = [literal_ip]

        if not ips:
            raise SSRFConnectionError(f"No IP addresses resolved for {host!r}")

        for ip in ips:
            reason = self.ip_block_reason(ip)
            if reason is not None:
                raise SSRFBlockedError(f"Blocked IP {ip} for host {host!r}: {reason}")

        return ips


@contextlib.contextmanager
def _map_httpcore_exceptions() -> Iterator[None]:
    """Translate raw httpcore exceptions into the SafeHttpError hierarchy (E-01)."""
    try:
        yield
    except SafeHttpError:
        raise
    except httpcore.TimeoutException as exc:
        raise SafeHttpTimeoutError(f"{type(exc).__name__}: {exc}") from exc
    except (httpcore.NetworkError, httpcore.ProxyError) as exc:
        raise SSRFConnectionError(f"{type(exc).__name__}: {exc}") from exc
    except (httpcore.ProtocolError, httpcore.UnsupportedProtocol) as exc:
        raise SafeHttpError(f"{type(exc).__name__}: {exc}") from exc


class SafeNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore AsyncNetworkBackend that validates DNS and pins the literal IP at connect_tcp()."""

    def __init__(
        self,
        policy: SSRFPolicy,
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self.policy = policy
        self.inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore AsyncNetworkBackend signature
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        ips = await self.policy.resolve_and_validate(host, port)
        last_exc: Exception | None = None
        for ip in ips:
            try:
                return await self.inner.connect_tcp(
                    str(ip),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (OSError, httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
                continue
        detail = f" ({type(last_exc).__name__}: {last_exc})" if last_exc is not None else ""
        raise SSRFConnectionError(
            f"Unable to connect to any validated IP for {host!r}:{port}{detail}"
        ) from last_exc

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore AsyncNetworkBackend signature
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        raise SSRFBlockedError("Unix domain sockets are forbidden")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


class _PinnedStream(httpx.AsyncByteStream):
    def __init__(self, core_response: httpcore.Response) -> None:
        self._core_response = core_response

    async def __aiter__(self) -> AsyncIterator[bytes]:
        with _map_httpcore_exceptions():
            async for chunk in self._core_response.aiter_stream():
                yield chunk

    async def aclose(self) -> None:
        with _map_httpcore_exceptions():
            await self._core_response.aclose()


class SafeTransport(httpx.AsyncBaseTransport):
    """httpx AsyncBaseTransport backed by httpcore.AsyncConnectionPool + SafeNetworkBackend."""

    def __init__(
        self,
        policy: SSRFPolicy,
        *,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.backend = SafeNetworkBackend(policy=policy, inner=network_backend)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context or ssl.create_default_context(),
            max_connections=20,
            max_keepalive_connections=10,
            network_backend=self.backend,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        core_req = httpcore.Request(
            method=request.method.encode("ascii"),
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            extensions=request.extensions,
        )
        with _map_httpcore_exceptions():
            core_resp = await self._pool.handle_async_request(core_req)
        return httpx.Response(
            status_code=core_resp.status,
            headers=core_resp.headers,
            stream=_PinnedStream(core_resp),
            extensions=core_resp.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        with _map_httpcore_exceptions():
            await self._pool.aclose()


@dataclass(frozen=True, slots=True)
class FetchResult:
    status_code: int
    final_url: str
    permanent_redirect_url: str | None
    headers: dict[str, str]
    body_bytes: bytes
    content_type: str | None


def _normalize_content_encoding(raw_header: str | None) -> str:
    if not raw_header or not raw_header.strip():
        return "identity"
    parts = [part.strip().lower() for part in raw_header.split(",") if part.strip()]
    if len(parts) != 1:
        raise UnsupportedEncodingError(f"Chained Content-Encoding is unsupported: {raw_header!r}")
    enc = parts[0]
    if enc in ("identity", "gzip", "x-gzip", "deflate"):
        return enc
    raise UnsupportedEncodingError(f"Unsupported Content-Encoding: {enc!r}")


async def _read_bounded_body(
    response: httpx.Response,
    *,
    encoding: str,
    max_bytes: int,
) -> bytes:
    content_length_raw = response.headers.get("content-length")
    if content_length_raw:
        try:
            if int(content_length_raw.strip()) > max_bytes:
                raise ResponseTooLargeError(
                    f"Content-Length {content_length_raw} exceeds limit {max_bytes}"
                )
        except ValueError:
            pass

    wire_total = 0
    out_chunks: list[bytes] = []

    if encoding == "identity":
        async for chunk in response.aiter_raw():
            wire_total += len(chunk)
            if wire_total > max_bytes:
                raise ResponseTooLargeError(f"Response exceeded {max_bytes} bytes")
            out_chunks.append(chunk)
        return b"".join(out_chunks)

    if encoding in ("gzip", "x-gzip"):
        decompressed_total = 0
        dec = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
        async for chunk in response.aiter_raw():
            wire_total += len(chunk)
            if wire_total > max_bytes:
                raise ResponseTooLargeError(f"Compressed response exceeded {max_bytes} bytes")

            data = chunk
            while data:
                remaining = max_bytes - decompressed_total
                out = dec.decompress(data, max_length=remaining + 1)
                if len(out) > remaining:
                    raise ResponseTooLargeError(
                        f"Decompressed gzip response exceeded {max_bytes} bytes"
                    )
                if out:
                    out_chunks.append(out)
                    decompressed_total += len(out)

                if dec.unconsumed_tail:
                    if decompressed_total >= max_bytes:
                        raise ResponseTooLargeError(
                            f"Decompressed gzip response exceeded {max_bytes} bytes"
                        )
                    data = dec.unconsumed_tail
                elif dec.eof:
                    unused = dec.unused_data
                    dec = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
                    data = unused
                else:
                    data = b""

        remaining = max_bytes - decompressed_total
        tail = dec.flush(remaining + 1)
        if len(tail) > remaining:
            raise ResponseTooLargeError(f"Decompressed gzip response exceeded {max_bytes} bytes")
        if tail:
            out_chunks.append(tail)
        return b"".join(out_chunks)

    # deflate: try zlib-wrapped first, fallback to raw deflate on first-chunk header failure
    decompressed_total = 0
    dec = zlib.decompressobj(wbits=zlib.MAX_WBITS)
    tried_raw_fallback = False
    first_chunk_seen = False

    async for chunk in response.aiter_raw():
        wire_total += len(chunk)
        if wire_total > max_bytes:
            raise ResponseTooLargeError(f"Compressed response exceeded {max_bytes} bytes")
        if not chunk:
            continue

        data = chunk
        while data:
            remaining = max_bytes - decompressed_total
            try:
                out = dec.decompress(data, max_length=remaining + 1)
            except zlib.error:
                if not first_chunk_seen and not tried_raw_fallback:
                    tried_raw_fallback = True
                    dec = zlib.decompressobj(wbits=-zlib.MAX_WBITS)
                    out = dec.decompress(data, max_length=remaining + 1)
                else:
                    raise
            first_chunk_seen = True
            if len(out) > remaining:
                raise ResponseTooLargeError(
                    f"Decompressed deflate response exceeded {max_bytes} bytes"
                )
            if out:
                out_chunks.append(out)
                decompressed_total += len(out)

            if dec.unconsumed_tail:
                if decompressed_total >= max_bytes:
                    raise ResponseTooLargeError(
                        f"Decompressed deflate response exceeded {max_bytes} bytes"
                    )
                data = dec.unconsumed_tail
            else:
                data = b""

    remaining = max_bytes - decompressed_total
    tail = dec.flush(remaining + 1)
    if len(tail) > remaining:
        raise ResponseTooLargeError(f"Decompressed deflate response exceeded {max_bytes} bytes")
    if tail:
        out_chunks.append(tail)
    return b"".join(out_chunks)


class SafeHttpClient:
    """Shared SSRF-hardened async HTTP client for all SmartRSS outbound fetches."""

    def __init__(
        self,
        policy: SSRFPolicy | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_redirects: int = 5,
        default_max_bytes: int = 5_000_000,
        default_total_timeout: float = 30.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.policy = policy or SSRFPolicy()
        self.max_redirects = max_redirects
        self.default_max_bytes = default_max_bytes
        self.default_total_timeout = default_total_timeout
        self.user_agent = user_agent
        self._transport = transport or SafeTransport(self.policy)
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            transport=self._transport,
            timeout=httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SafeHttpClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
        total_timeout: float | None = None,
    ) -> FetchResult:
        """Fetch a URL with SSRF validation, manual redirect revalidation, and bounded streaming."""
        effective_max_bytes = max_bytes if max_bytes is not None else self.default_max_bytes
        effective_timeout = (
            total_timeout if total_timeout is not None else self.default_total_timeout
        )

        req_headers = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
        }
        if headers:
            for k, v in headers.items():
                if k.lower() != "accept-encoding":
                    req_headers[k] = v

        current_url = url
        redirects_followed = 0
        all_permanent = True

        async with asyncio.timeout(effective_timeout):
            while True:
                self.policy.validate_url(current_url)

                async with self._client.stream("GET", current_url, headers=req_headers) as response:
                    status = response.status_code
                    if status in REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise SafeHttpError(
                                f"Redirect response ({status}) missing Location header"
                            )
                        if redirects_followed >= self.max_redirects:
                            raise TooManyRedirectsError(
                                f"Exceeded max redirects ({self.max_redirects}) from {url}"
                            )
                        redirects_followed += 1
                        if status not in PERMANENT_REDIRECT_STATUSES:
                            all_permanent = False
                        current_url = urljoin(current_url, location.strip())
                        continue

                    encoding = _normalize_content_encoding(response.headers.get("content-encoding"))
                    body_bytes = await _read_bounded_body(
                        response,
                        encoding=encoding,
                        max_bytes=effective_max_bytes,
                    )
                    norm_headers = {k.lower(): v for k, v in response.headers.items()}
                    permanent_url = (
                        current_url if (redirects_followed > 0 and all_permanent) else None
                    )
                    return FetchResult(
                        status_code=status,
                        final_url=current_url,
                        permanent_redirect_url=permanent_url,
                        headers=norm_headers,
                        body_bytes=body_bytes,
                        content_type=norm_headers.get("content-type"),
                    )


class DomainRateLimiter:
    """Per-domain politeness limiter: concurrency=1 and min_interval_seconds between requests."""

    def __init__(
        self,
        *,
        min_interval_seconds: float = 1.5,
        clock: Callable[[], float] | None = None,
        max_idle_seconds: float = 600.0,
        prune_threshold: int = 256,
    ) -> None:
        self._min_interval = min_interval_seconds
        self._clock = clock or time.monotonic
        self._max_idle_seconds = max_idle_seconds
        self._prune_threshold = prune_threshold
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}

    def prune_idle(self) -> int:
        """Evict unlocked idle domain entries older than max_idle_seconds (I-23)."""
        cutoff = self._clock() - self._max_idle_seconds
        stale = [
            host
            for host, last_ts in list(self._last_request.items())
            if last_ts < cutoff and not self._locks.get(host, asyncio.Lock()).locked()
        ]
        for host in stale:
            self._last_request.pop(host, None)
            self._locks.pop(host, None)
        return len(stale)

    def _get_lock(self, host: str) -> asyncio.Lock:
        if len(self._locks) >= self._prune_threshold:
            self.prune_idle()
        lock = self._locks.get(host)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[host] = lock
        return lock

    async def acquire_slot(self, host: str | None) -> _DomainSlot:
        """Return an async context manager holding the domain slot."""
        normalized = (host or "_unknown").strip().lower()
        return _DomainSlot(self, normalized)


class _DomainSlot:
    def __init__(self, limiter: DomainRateLimiter, host: str) -> None:
        self._limiter = limiter
        self._host = host
        self._lock = limiter._get_lock(host)

    async def __aenter__(self) -> None:
        await self._lock.acquire()
        try:
            last = self._limiter._last_request.get(self._host)
            if last is not None:
                elapsed = self._limiter._clock() - last
                wait = self._limiter._min_interval - elapsed
                if wait > 0:
                    await asyncio.sleep(wait)
        except BaseException:
            self._lock.release()
            raise

    async def __aexit__(self, *_exc: object) -> None:
        self._limiter._last_request[self._host] = self._limiter._clock()
        self._lock.release()
