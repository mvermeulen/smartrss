"""Boundary tests for SafeHttpClient (IP pinning, redirects, gzip bombs, deflate, timeouts)."""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import time
import zlib
from collections.abc import Callable
from typing import Any

import httpcore
import pytest

from smartrss.security.safe_http import (
    DomainRateLimiter,
    ResponseTooLargeError,
    SafeHttpClient,
    SafeTransport,
    SSRFBlockedError,
    SSRFPolicy,
    TooManyRedirectsError,
    UnsupportedEncodingError,
)


class _FakeNetworkStream(httpcore.AsyncNetworkStream):
    """In-memory raw HTTP/1.1 byte stream recording requests and streaming chunks."""

    def __init__(
        self,
        response_handler: Callable[[bytes], list[bytes]],
        *,
        chunk_delay: float = 0.0,
        tls_hostnames: list[str | None] | None = None,
    ) -> None:
        self._handler = response_handler
        self._chunk_delay = chunk_delay
        self._tls_hostnames = tls_hostnames
        self._req_buf = bytearray()
        self._chunks: list[bytes] | None = None
        self._idx = 0
        self.closed = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        if self._chunks is None:
            self._chunks = self._handler(bytes(self._req_buf))
            self._idx = 0
        if self._idx >= len(self._chunks):
            return b""
        if self._chunk_delay > 0:
            await asyncio.sleep(self._chunk_delay)
        chunk = self._chunks[self._idx]
        if len(chunk) <= max_bytes:
            self._idx += 1
            return chunk
        self._chunks[self._idx] = chunk[max_bytes:]
        return chunk[:max_bytes]

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        self._req_buf.extend(buffer)

    async def aclose(self) -> None:
        self.closed = True

    async def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        if self._tls_hostnames is not None:
            self._tls_hostnames.append(server_hostname)
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


class _RecordingNetworkBackend(httpcore.AsyncNetworkBackend):
    """Records literal IP/port passed to connect_tcp() and raw HTTP requests."""

    def __init__(
        self,
        handler: Callable[[str, int, bytes], list[bytes]],
        *,
        chunk_delay: float = 0.0,
    ) -> None:
        self._handler = handler
        self._chunk_delay = chunk_delay
        self.connect_calls: list[tuple[str, int]] = []
        self.raw_requests: list[bytes] = []
        self.tls_hostnames: list[str | None] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connect_calls.append((host, port))

        def _on_req(raw: bytes) -> list[bytes]:
            self.raw_requests.append(raw)
            return self._handler(host, port, raw)

        return _FakeNetworkStream(
            _on_req,
            chunk_delay=self._chunk_delay,
            tls_hostnames=self.tls_hostnames,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        raise AssertionError("unix socket must not be called")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def _http_response(
    status: int,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    *,
    reason: str = "OK",
) -> bytes:
    hdrs = {"Connection": "close"}
    if headers:
        hdrs.update(headers)
    if "Content-Length" not in hdrs and "Transfer-Encoding" not in hdrs:
        hdrs["Content-Length"] = str(len(body))
    lines = [f"HTTP/1.1 {status} {reason}"]
    lines.extend(f"{k}: {v}" for k, v in hdrs.items())
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def _make_dns(mapping: dict[str, list[str]]) -> Callable[[str, int], Any]:
    async def _resolve(
        host: str, _port: int
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        return [ipaddress.ip_address(ip) for ip in mapping.get(host, ["93.184.216.34"])]

    return _resolve


async def test_ip_pinning_preserves_host_header_and_connects_to_literal_ip() -> None:
    policy = SSRFPolicy(resolver=_make_dns({"feeds.example.com": ["93.184.216.34"]}))
    backend = _RecordingNetworkBackend(
        lambda _ip, _port, _raw: [
            _http_response(200, b"hello world", {"Content-Type": "text/plain"})
        ]
    )
    async with SafeHttpClient(
        policy=policy, transport=SafeTransport(policy, network_backend=backend)
    ) as client:
        res = await client.fetch("http://feeds.example.com/rss.xml")

    assert res.status_code == 200
    assert res.body_bytes == b"hello world"
    assert res.permanent_redirect_url is None
    # connect_tcp received the LITERAL pinned IP, while the HTTP Host header preserved hostname
    assert backend.connect_calls == [("93.184.216.34", 80)]
    raw_req = backend.raw_requests[0].decode("latin-1")
    assert "Host: feeds.example.com\r\n" in raw_req
    assert "Accept-Encoding: gzip, deflate\r\n" in raw_req


async def test_dual_homed_dns_and_private_ip_redirect_blocked() -> None:
    dns = _make_dns(
        {
            "dual.example.com": ["93.184.216.34", "127.0.0.1"],
            "redir.example.com": ["93.184.216.34"],
            "internal.evil": ["192.168.1.1"],
            "metadata.evil": ["169.254.169.254"],
        }
    )
    policy = SSRFPolicy(resolver=dns)

    def _handler(_ip: str, _port: int, raw: bytes) -> list[bytes]:
        if b"/to-lan" in raw:
            return [_http_response(302, b"", {"Location": "http://internal.evil/admin"})]
        if b"/to-meta" in raw:
            return [_http_response(301, b"", {"Location": "http://metadata.evil/latest"})]
        return [_http_response(200, b"ok")]

    backend = _RecordingNetworkBackend(_handler)
    async with SafeHttpClient(
        policy=policy, transport=SafeTransport(policy, network_backend=backend)
    ) as client:
        with pytest.raises(SSRFBlockedError):
            await client.fetch("http://dual.example.com/feed")
        assert backend.connect_calls == []

        with pytest.raises(SSRFBlockedError):
            await client.fetch("http://redir.example.com/to-lan")

        with pytest.raises(SSRFBlockedError):
            await client.fetch("http://redir.example.com/to-meta")

    # Never connected to any private IP
    assert all(ip == "93.184.216.34" for ip, _ in backend.connect_calls)


async def test_redirect_chains_permanent_vs_mixed_and_max_hops() -> None:
    policy = SSRFPolicy(resolver=_make_dns({"a.example.com": ["93.184.216.34"]}))

    def _handler(_ip: str, _port: int, raw: bytes) -> list[bytes]:
        first_line = raw.split(b"\r\n", 1)[0].decode("ascii")
        path = first_line.split(" ")[1]
        if path == "/perm1":
            return [_http_response(301, b"", {"Location": "/perm2"})]
        if path == "/perm2":
            return [_http_response(308, b"", {"Location": "/final"})]
        if path == "/mixed1":
            return [_http_response(301, b"", {"Location": "/mixed2"})]
        if path == "/mixed2":
            return [_http_response(302, b"", {"Location": "/final"})]
        if path.startswith("/loop"):
            step = int(path.removeprefix("/loop"))
            return [_http_response(302, b"", {"Location": f"/loop{step + 1}"})]
        return [_http_response(200, b"done")]

    backend = _RecordingNetworkBackend(_handler)
    async with SafeHttpClient(
        policy=policy, transport=SafeTransport(policy, network_backend=backend)
    ) as client:
        res_perm = await client.fetch("http://a.example.com/perm1")
        assert res_perm.final_url == "http://a.example.com/final"
        assert res_perm.permanent_redirect_url == "http://a.example.com/final"

        res_mixed = await client.fetch("http://a.example.com/mixed1")
        assert res_mixed.final_url == "http://a.example.com/final"
        assert res_mixed.permanent_redirect_url is None

        with pytest.raises(TooManyRedirectsError):
            await client.fetch("http://a.example.com/loop0")


async def test_streaming_gzip_bomb_aborted_at_max_bytes() -> None:
    # Single ~20 KB gzip payload that expands to 20 MiB of zeros
    bomb_wire = gzip.compress(b"\x00" * (20 * 1024 * 1024), compresslevel=9)
    assert len(bomb_wire) < 65_536

    policy = SSRFPolicy(resolver=_make_dns({"bomb.example.com": ["93.184.216.34"]}))
    backend = _RecordingNetworkBackend(
        lambda _ip, _port, _raw: [_http_response(200, bomb_wire, {"Content-Encoding": "gzip"})]
    )
    async with SafeHttpClient(
        policy=policy, transport=SafeTransport(policy, network_backend=backend)
    ) as client:
        with pytest.raises(ResponseTooLargeError):
            await client.fetch("http://bomb.example.com/feed", max_bytes=100_000)


async def test_multi_member_gzip_and_zlib_vs_raw_deflate_and_unsupported_encoding() -> None:
    multi_gzip = gzip.compress(b"part-one-") + gzip.compress(b"part-two")
    zlib_deflate = zlib.compress(b"hello-zlib-deflate")
    raw_comp = zlib.compressobj(level=6, wbits=-zlib.MAX_WBITS)
    raw_deflate = raw_comp.compress(b"hello-raw-deflate") + raw_comp.flush()

    policy = SSRFPolicy(resolver=_make_dns({"enc.example.com": ["93.184.216.34"]}))

    def _handler(_ip: str, _port: int, raw: bytes) -> list[bytes]:
        path = raw.split(b"\r\n", 1)[0].decode("ascii").split(" ")[1]
        if path == "/multi-gz":
            return [_http_response(200, multi_gzip, {"Content-Encoding": "gzip"})]
        if path == "/zlib":
            return [_http_response(200, zlib_deflate, {"Content-Encoding": "deflate"})]
        if path == "/raw-deflate":
            return [_http_response(200, raw_deflate, {"Content-Encoding": "deflate"})]
        if path == "/br":
            return [_http_response(200, b"br-bytes", {"Content-Encoding": "br"})]
        if path == "/chained":
            return [_http_response(200, multi_gzip, {"Content-Encoding": "gzip, gzip"})]
        if path == "/huge-cl":
            return [_http_response(200, b"x", {"Content-Length": "9999999"})]
        return [_http_response(200, b"a" * 500)]

    backend = _RecordingNetworkBackend(_handler)
    async with SafeHttpClient(
        policy=policy, transport=SafeTransport(policy, network_backend=backend)
    ) as client:
        assert (
            await client.fetch("http://enc.example.com/multi-gz")
        ).body_bytes == b"part-one-part-two"
        assert (
            await client.fetch("http://enc.example.com/zlib")
        ).body_bytes == b"hello-zlib-deflate"
        assert (
            await client.fetch("http://enc.example.com/raw-deflate")
        ).body_bytes == b"hello-raw-deflate"

        with pytest.raises(UnsupportedEncodingError):
            await client.fetch("http://enc.example.com/br")
        with pytest.raises(UnsupportedEncodingError):
            await client.fetch("http://enc.example.com/chained")
        with pytest.raises(ResponseTooLargeError):
            await client.fetch("http://enc.example.com/huge-cl", max_bytes=1000)
        with pytest.raises(ResponseTooLargeError):
            await client.fetch("http://enc.example.com/plain", max_bytes=100)


async def test_slow_drip_total_timeout_aborts() -> None:
    policy = SSRFPolicy(resolver=_make_dns({"slow.example.com": ["93.184.216.34"]}))
    header = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
    chunks = [header] + [b"5\r\nhello\r\n"] * 20 + [b"0\r\n\r\n"]
    backend = _RecordingNetworkBackend(lambda _ip, _port, _raw: list(chunks), chunk_delay=0.08)

    async with SafeHttpClient(
        policy=policy, transport=SafeTransport(policy, network_backend=backend)
    ) as client:
        with pytest.raises(TimeoutError):
            await client.fetch("http://slow.example.com/drip", total_timeout=0.2)


async def test_domain_rate_limiter_enforces_spacing_and_concurrency() -> None:
    limiter = DomainRateLimiter(min_interval_seconds=0.12)
    timestamps: list[float] = []

    async def _hit(host: str) -> None:
        slot = await limiter.acquire_slot(host)
        async with slot:
            timestamps.append(time.monotonic())

    await asyncio.gather(_hit("same.example.com"), _hit("same.example.com"))
    assert len(timestamps) == 2
    assert (timestamps[1] - timestamps[0]) >= 0.10


async def test_safe_network_backend_failover_unix_block_and_deflate_bomb() -> None:
    from smartrss.security.safe_http import (
        SafeHttpError,
        SafeNetworkBackend,
        SSRFConnectionError,
    )

    # 1. connect_unix_socket is always blocked; connect_tcp tries second IP on OSError
    attempts: list[str] = []

    class _FailFirstBackend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,  # noqa: ASYNC109
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            attempts.append(host)
            if host == "93.184.216.34":
                raise OSError("connection refused")
            return _FakeNetworkStream(lambda _raw: [_http_response(200, b"fallback-ip-ok")])

        async def connect_unix_socket(
            self,
            path: str,
            timeout: float | None = None,  # noqa: ASYNC109
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            raise AssertionError("unreachable")

        async def sleep(self, seconds: float) -> None:
            await asyncio.sleep(seconds)

    policy = SSRFPolicy(resolver=_make_dns({"multi.example.com": ["93.184.216.34", "1.1.1.1"]}))
    safe_backend = SafeNetworkBackend(policy=policy, inner=_FailFirstBackend())
    with pytest.raises(SSRFBlockedError):
        await safe_backend.connect_unix_socket("/var/run/docker.sock")

    async with SafeHttpClient(
        policy=policy,
        transport=SafeTransport(policy, network_backend=_FailFirstBackend()),
    ) as client:
        res = await client.fetch("http://multi.example.com/ok")
        assert res.body_bytes == b"fallback-ip-ok"
    assert attempts == ["93.184.216.34", "1.1.1.1"]

    # All IPs failing -> SSRFConnectionError
    class _AlwaysFailBackend(_FailFirstBackend):
        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,  # noqa: ASYNC109
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            raise OSError("refused")

    all_fail = SafeNetworkBackend(policy=policy, inner=_AlwaysFailBackend())
    with pytest.raises(SSRFConnectionError):
        await all_fail.connect_tcp("multi.example.com", 80)

    # 2. Deflate bomb aborted at max_bytes & redirect missing Location raises SafeHttpError
    deflate_bomb = zlib.compress(b"\x00" * (10 * 1024 * 1024), level=9)
    p2 = SSRFPolicy(resolver=_make_dns({"d.example.com": ["93.184.216.34"]}))

    def _d_handler(_ip: str, _port: int, raw: bytes) -> list[bytes]:
        if b"/missing-loc" in raw:
            return [_http_response(302, b"")]
        return [_http_response(200, deflate_bomb, {"Content-Encoding": "deflate"})]

    b2 = _RecordingNetworkBackend(_d_handler)
    async with SafeHttpClient(policy=p2, transport=SafeTransport(p2, network_backend=b2)) as client:
        with pytest.raises(ResponseTooLargeError):
            await client.fetch("http://d.example.com/bomb", max_bytes=50_000)
        with pytest.raises(SafeHttpError):
            await client.fetch("http://d.example.com/missing-loc")

    # 3. Literal IP validation & disallowed port & invalid port syntax
    assert await p2.resolve_and_validate("93.184.216.34", 80) == [
        ipaddress.IPv4Address("93.184.216.34")
    ]
    with pytest.raises(SSRFBlockedError):
        await p2.resolve_and_validate("127.0.0.1", 80)
    with pytest.raises(SSRFBlockedError):
        await p2.resolve_and_validate("93.184.216.34", 22)
    with pytest.raises(SSRFBlockedError):
        p2.validate_url("http://example.com:99999/rss")


async def test_https_tls_sni_pinning_chunked_identity_cap_and_limiter_pruning() -> None:
    """I-07 & I-23: HTTPS preserves TLS SNI/Host while pinning literal IP; chunked identity cap."""
    policy = SSRFPolicy(resolver=_make_dns({"secure.example.com": ["93.184.216.34"]}))

    def _handler(_ip: str, _port: int, raw: bytes) -> list[bytes]:
        path = raw.split(b"\r\n", 1)[0].decode("ascii").split(" ")[1]
        if path == "/chunked-huge":
            hdr = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            chunk = b"400\r\n" + (b"A" * 1024) + b"\r\n"
            return [hdr, chunk, chunk, chunk, b"0\r\n\r\n"]
        if path == "/wire-cap-gz":
            # Compressed payload itself exceeds max_bytes without Content-Length
            wire = gzip.compress(bytes(range(256)) * 20)
            hdr = (
                b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n"
                b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            )
            return [hdr, f"{len(wire):x}\r\n".encode("ascii") + wire + b"\r\n", b"0\r\n\r\n"]
        if path == "/raw-deflate-bomb":
            comp = zlib.compressobj(level=9, wbits=-zlib.MAX_WBITS)
            raw_bomb = comp.compress(b"\x00" * (2 * 1024 * 1024)) + comp.flush()
            return [_http_response(200, raw_bomb, {"Content-Encoding": "deflate"})]
        return [_http_response(200, b"tls-ok", {"Content-Type": "text/plain"})]

    backend = _RecordingNetworkBackend(_handler)
    async with SafeHttpClient(
        policy=policy, transport=SafeTransport(policy, network_backend=backend)
    ) as client:
        # Caller headers preserved, except Accept-Encoding cannot be overridden
        res = await client.fetch(
            "https://secure.example.com/feed",
            headers={"If-None-Match": '"abc"', "Accept-Encoding": "br"},
        )
        assert res.status_code == 200
        assert res.body_bytes == b"tls-ok"
        assert backend.connect_calls[0] == ("93.184.216.34", 443)
        assert backend.tls_hostnames[0] == "secure.example.com"
        raw0 = backend.raw_requests[0].decode("latin-1")
        assert "Host: secure.example.com\r\n" in raw0
        assert "Accept-Encoding: gzip, deflate\r\n" in raw0
        assert 'If-None-Match: "abc"\r\n' in raw0

        # Streaming identity wire cap without Content-Length
        with pytest.raises(ResponseTooLargeError):
            await client.fetch("https://secure.example.com/chunked-huge", max_bytes=1500)

        # Streaming compressed wire cap
        with pytest.raises(ResponseTooLargeError):
            await client.fetch("https://secure.example.com/wire-cap-gz", max_bytes=100)

        # Raw deflate decompression bomb
        with pytest.raises(ResponseTooLargeError):
            await client.fetch("https://secure.example.com/raw-deflate-bomb", max_bytes=50_000)

        # Valid large gzip & deflate streams exercising multi-step decompression (unconsumed_tail)
        payload = b"SmartRSS streaming decompression test line!\n" * 6000  # ~264 KB
        gz_payload = gzip.compress(payload)
        df_payload = zlib.compress(payload)
        b_big = _RecordingNetworkBackend(
            lambda _ip, _p, raw: [
                _http_response(
                    200,
                    gz_payload if b"/big-gz" in raw else df_payload,
                    {"Content-Encoding": "gzip" if b"/big-gz" in raw else "deflate"},
                )
            ]
        )
        async with SafeHttpClient(
            policy=policy, transport=SafeTransport(policy, network_backend=b_big)
        ) as big_client:
            assert (
                await big_client.fetch("http://secure.example.com/big-gz", max_bytes=len(payload))
            ).body_bytes == payload
            assert (
                await big_client.fetch("http://secure.example.com/big-df", max_bytes=len(payload))
            ).body_bytes == payload
            # Exact 1-byte overflow triggers ResponseTooLargeError
            with pytest.raises(ResponseTooLargeError):
                await big_client.fetch(
                    "http://secure.example.com/big-gz", max_bytes=len(payload) - 1
                )
            with pytest.raises(ResponseTooLargeError):
                await big_client.fetch(
                    "http://secure.example.com/big-df", max_bytes=len(payload) - 1
                )

    # I-23: DomainRateLimiter prunes idle hosts
    now = [100.0]
    limiter = DomainRateLimiter(
        min_interval_seconds=0.0,
        clock=lambda: now[0],
        max_idle_seconds=60.0,
        prune_threshold=2,
    )
    for host in ("h1.example.com", "h2.example.com"):
        slot = await limiter.acquire_slot(host)
        async with slot:
            pass
    now[0] += 120.0
    slot3 = await limiter.acquire_slot("h3.example.com")
    async with slot3:
        pass
    assert "h1.example.com" not in limiter._locks
    assert "h2.example.com" not in limiter._locks
    assert "h3.example.com" in limiter._locks


async def test_e01_connect_tcp_failover_httpcore_errors_and_real_anyio() -> None:
    """E-01 (a): SafeNetworkBackend fails over on httpcore.ConnectError / ConnectTimeout
    and with real httpcore.AnyIOBackend against a local IPv4-only server."""
    attempts: list[str] = []

    class _HttpcoreFailFirstBackend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,  # noqa: ASYNC109
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            attempts.append(host)
            if host == "93.184.216.34":
                raise httpcore.ConnectError("All connection attempts failed")
            if host == "1.0.0.1":
                raise httpcore.ConnectTimeout("timed out")
            return _FakeNetworkStream(lambda _raw: [_http_response(200, b"httpcore-failover-ok")])

        async def connect_unix_socket(
            self,
            path: str,
            timeout: float | None = None,  # noqa: ASYNC109
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            raise AssertionError("unreachable")

        async def sleep(self, seconds: float) -> None:
            await asyncio.sleep(seconds)

    policy = SSRFPolicy(
        resolver=_make_dns({"dual.example.com": ["93.184.216.34", "1.0.0.1", "1.1.1.1"]})
    )
    async with SafeHttpClient(
        policy=policy,
        transport=SafeTransport(policy, network_backend=_HttpcoreFailFirstBackend()),
    ) as client:
        res = await client.fetch("http://dual.example.com/feed")
        assert res.status_code == 200
        assert res.body_bytes == b"httpcore-failover-ok"
    assert attempts == ["93.184.216.34", "1.0.0.1", "1.1.1.1"]

    # Real httpcore.AnyIOBackend failover: bind IPv4-only server on 127.0.0.1:<port>,
    # resolver returns [::1 (refused/unreachable on that port), 127.0.0.1 (listening)].
    async def _serve_once(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(4096)
            writer.write(_http_response(200, b"real-anyio-v4-ok"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(_serve_once, host="127.0.0.1", port=0)
    try:
        sock = server.sockets[0]
        port = int(sock.getsockname()[1])

        class _TestPermissiveLoopbackPolicy(SSRFPolicy):
            def ip_block_reason(
                self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address
            ) -> str | None:
                return None

        real_policy = _TestPermissiveLoopbackPolicy(
            allowed_ports=(port,),
            resolver=_make_dns({"local-dual.test": ["::1", "127.0.0.1"]}),
        )
        async with SafeHttpClient(
            policy=real_policy,
            transport=SafeTransport(real_policy, network_backend=httpcore.AnyIOBackend()),
        ) as real_client:
            res_real = await real_client.fetch(f"http://local-dual.test:{port}/rss")
            assert res_real.status_code == 200
            assert res_real.body_bytes == b"real-anyio-v4-ok"
    finally:
        server.close()
        await server.wait_closed()


async def test_e01_all_ips_fail_with_httpcore_errors_raises_ssrf_connection_error() -> None:
    """E-01 (b): When all validated IPs fail with real httpcore errors, SSRFConnectionError
    is raised chained from the last httpcore exception."""
    from smartrss.security.safe_http import SafeNetworkBackend, SSRFConnectionError

    class _AllHttpcoreFailBackend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,  # noqa: ASYNC109
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            if host == "93.184.216.34":
                raise httpcore.ConnectError("first refused")
            raise httpcore.ConnectTimeout("second timed out")

        async def connect_unix_socket(
            self,
            path: str,
            timeout: float | None = None,  # noqa: ASYNC109
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            raise AssertionError("unreachable")

        async def sleep(self, seconds: float) -> None:
            await asyncio.sleep(seconds)

    policy = SSRFPolicy(resolver=_make_dns({"dead.example.com": ["93.184.216.34", "1.1.1.1"]}))
    backend = SafeNetworkBackend(policy=policy, inner=_AllHttpcoreFailBackend())
    with pytest.raises(SSRFConnectionError) as exc_info:
        await backend.connect_tcp("dead.example.com", 80)
    assert isinstance(exc_info.value.__cause__, httpcore.ConnectTimeout)

    # Also verify via full SafeHttpClient.fetch() with real AnyIOBackend against a closed port
    import socket as _socket

    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        closed_port = int(s.getsockname()[1])

    class _PermissivePolicy(SSRFPolicy):
        def ip_block_reason(self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
            return None

    closed_policy = _PermissivePolicy(
        allowed_ports=(closed_port,),
        resolver=_make_dns({"closed.test": ["127.0.0.1"]}),
    )
    async with SafeHttpClient(
        policy=closed_policy,
        transport=SafeTransport(closed_policy, network_backend=httpcore.AnyIOBackend()),
    ) as client:
        with pytest.raises(SSRFConnectionError) as real_exc:
            await client.fetch(f"http://closed.test:{closed_port}/rss")
        assert "Unable to connect to any validated IP" in str(real_exc.value)
        assert "ConnectError" in str(real_exc.value)


async def test_e01_safe_transport_and_pinned_stream_map_httpcore_exceptions() -> None:
    """E-01 (c): SafeTransport and _PinnedStream map httpcore errors into SafeHttpError."""
    from smartrss.security.safe_http import (
        SafeHttpError,
        SafeHttpTimeoutError,
        SSRFConnectionError,
        _PinnedStream,
    )

    policy = SSRFPolicy(resolver=_make_dns({"err.example.com": ["93.184.216.34"]}))

    # 1. TLS start_tls raises httpcore.ConnectError (exact 03:30 soak path) -> SSRFConnectionError
    class _FailTlsStream(_FakeNetworkStream):
        async def start_tls(
            self,
            ssl_context: Any,
            server_hostname: str | None = None,
            timeout: float | None = None,  # noqa: ASYNC109
        ) -> httpcore.AsyncNetworkStream:
            raise httpcore.ConnectError("TLS handshake failed")

    class _FailTlsBackend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,  # noqa: ASYNC109
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            return _FailTlsStream(lambda _raw: [])

        async def connect_unix_socket(
            self,
            path: str,
            timeout: float | None = None,  # noqa: ASYNC109
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            raise AssertionError("unreachable")

        async def sleep(self, seconds: float) -> None:
            await asyncio.sleep(seconds)

    async with SafeHttpClient(
        policy=policy,
        transport=SafeTransport(policy, network_backend=_FailTlsBackend()),
    ) as client:
        with pytest.raises(SSRFConnectionError) as tls_exc:
            await client.fetch("https://err.example.com/rss")
        assert isinstance(tls_exc.value.__cause__, httpcore.ConnectError)

    # 2. ReadTimeout and RemoteProtocolError from pool -> SafeHttpTimeoutError / SafeHttpError
    class _RaiseOnReadStream(_FakeNetworkStream):
        def __init__(self, exc: Exception, *, fail_after_headers: bool = False) -> None:
            super().__init__(lambda _raw: [])
            self._exc = exc
            self._fail_after_headers = fail_after_headers
            self._sent_headers = False

        async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
            if self._fail_after_headers and not self._sent_headers:
                self._sent_headers = True
                return b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\nhello"
            raise self._exc

    class _SingleStreamBackend(httpcore.AsyncNetworkBackend):
        def __init__(self, stream_factory: Callable[[], httpcore.AsyncNetworkStream]) -> None:
            self._stream_factory = stream_factory

        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,  # noqa: ASYNC109
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            return self._stream_factory()

        async def connect_unix_socket(
            self,
            path: str,
            timeout: float | None = None,  # noqa: ASYNC109
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            raise AssertionError("unreachable")

        async def sleep(self, seconds: float) -> None:
            await asyncio.sleep(seconds)

    # ReadTimeout during response headers (handle_async_request)
    async with SafeHttpClient(
        policy=policy,
        transport=SafeTransport(
            policy,
            network_backend=_SingleStreamBackend(
                lambda: _RaiseOnReadStream(httpcore.ReadTimeout("header timeout"))
            ),
        ),
    ) as client:
        with pytest.raises(SafeHttpTimeoutError) as rt_exc:
            await client.fetch("http://err.example.com/timeout")
        assert isinstance(rt_exc.value, SafeHttpError)
        assert isinstance(rt_exc.value.__cause__, httpcore.ReadTimeout)

    # RemoteProtocolError from server sending garbage HTTP
    async with SafeHttpClient(
        policy=policy,
        transport=SafeTransport(
            policy,
            network_backend=_SingleStreamBackend(
                lambda: _FakeNetworkStream(lambda _raw: [b"NOT-HTTP-GARBAGE\r\n\r\n"])
            ),
        ),
    ) as client:
        with pytest.raises(SafeHttpError) as proto_exc:
            await client.fetch("http://err.example.com/bad-proto")
        assert isinstance(proto_exc.value.__cause__, httpcore.RemoteProtocolError)

    # ReadTimeout mid-body during _PinnedStream.__aiter__
    async with SafeHttpClient(
        policy=policy,
        transport=SafeTransport(
            policy,
            network_backend=_SingleStreamBackend(
                lambda: _RaiseOnReadStream(
                    httpcore.ReadTimeout("body timeout"), fail_after_headers=True
                )
            ),
        ),
    ) as client:
        with pytest.raises(SafeHttpTimeoutError) as body_rt_exc:
            await client.fetch("http://err.example.com/body-timeout")
        assert isinstance(body_rt_exc.value.__cause__, httpcore.ReadTimeout)

    # _PinnedStream.aclose maps httpcore.ReadError -> SSRFConnectionError
    class _BrokenCloseCoreResponse:
        async def aiter_stream(self) -> Any:
            yield b""

        async def aclose(self) -> None:
            raise httpcore.ReadError("close error")

    pinned = _PinnedStream(_BrokenCloseCoreResponse())  # type: ignore[arg-type]
    with pytest.raises(SSRFConnectionError) as close_exc:
        await pinned.aclose()
    assert isinstance(close_exc.value.__cause__, httpcore.ReadError)
