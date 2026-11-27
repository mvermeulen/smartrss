"""Unit tests for SSRFPolicy URL and IP classification (ADR 0006, §7.2)."""

from __future__ import annotations

import ipaddress

import pytest

from smartrss.security.safe_http import SSRFBlockedError, SSRFConnectionError, SSRFPolicy


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "file:///etc/passwd",
        "ftp://example.com/feed.xml",
        "gopher://127.0.0.1:70/",
        "data:text/html,hello",
        "http://user:pass@example.com/rss",
        "http://example.com:8080/rss",
        "https://example.com:22/rss",
        "http://example.com/feed\r\nX-Injected: 1",
        "http:///no-host",
    ],
)
def test_validate_url_blocks_unsafe_urls(bad_url: str) -> None:
    policy = SSRFPolicy()
    with pytest.raises(SSRFBlockedError):
        policy.validate_url(bad_url)


def test_validate_url_accepts_http_and_https_default_ports() -> None:
    policy = SSRFPolicy()
    assert policy.validate_url("https://example.com/rss.xml") == ("https", "example.com", 443)
    assert policy.validate_url("http://example.com/rss.xml") == ("http", "example.com", 80)


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://www.latent.space/feed](https://www.latent.space/feed",
        "https://example.com/feed with space",
        "https://example.com/feed<tag>",
        'https://example.com/feed"quote',
        "https://example.com/feed{brace}",
        "https://example.com/feed|pipe",
        "https://example.com/feed\\slash",
        "https://example.com/feed^caret",
        "https://example.com/feed`tick",
        "https://example.com/feed[0]",
        "https://example.com/feed?a=b]",
        "https://example.com/feed#frag[1]",
        "https://example.com/feed/https://other.example/rss",
        "https://example.com/feed?next=http://other.example/rss",
    ],
)
def test_e10_validate_url_rejects_markdown_and_rfc3986_disallowed_chars(bad_url: str) -> None:
    policy = SSRFPolicy()
    with pytest.raises(SSRFBlockedError):
        policy.validate_url(bad_url)


def test_e10_validate_url_accepts_percent_encoded_and_ipv6_hosts() -> None:
    policy = SSRFPolicy()
    encoded_url = "https://example.com/feed%5D?q=a%20b&src=https%3A%2F%2Fother.test"
    assert policy.validate_url(encoded_url) == (
        "https",
        "example.com",
        443,
    )
    assert policy.validate_url("https://[2606:2800:220:1:248:1893:25c8:1946]/feed.xml") == (
        "https",
        "2606:2800:220:1:248:1893:25c8:1946",
        443,
    )


@pytest.mark.parametrize(
    "blocked_ip",
    [
        "127.0.0.1",
        "127.255.255.254",
        "::1",
        "10.0.0.1",
        "172.16.0.1",
        "172.17.0.2",
        "192.168.1.1",
        "169.254.169.254",
        "fe80::1",
        "100.64.0.1",
        "100.127.255.254",
        "fc00::1",
        "fd12:3456:789a::1",
        "::ffff:127.0.0.1",
        "::ffff:192.168.1.10",
        "::ffff:169.254.169.254",
        "64:ff9b::127.0.0.1",
        "64:ff9b::192.168.1.1",
        "64:ff9b::10.0.0.5",
        "64:ff9b:1::1",
        "2002:c0a8:0101::1",
        "2002:7f00:0001::1",
        "198.18.0.1",
        "192.0.2.1",
        "198.51.100.1",
        "203.0.113.1",
        "0.0.0.0",
        "224.0.0.1",
        "ff02::1",
    ],
)
def test_ip_block_reason_blocks_all_forbidden_ranges(blocked_ip: str) -> None:
    policy = SSRFPolicy()
    ip = ipaddress.ip_address(blocked_ip)
    assert policy.ip_block_reason(ip) is not None, f"Expected {blocked_ip} to be blocked"


@pytest.mark.parametrize(
    "public_ip",
    [
        "93.184.216.34",
        "1.1.1.1",
        "8.8.8.8",
        "2606:2800:220:1:248:1893:25c8:1946",
        "64:ff9b::93.184.216.34",
    ],
)
def test_ip_block_reason_allows_public_ips(public_ip: str) -> None:
    policy = SSRFPolicy()
    ip = ipaddress.ip_address(public_ip)
    assert policy.ip_block_reason(ip) is None


def test_extra_blocked_cidrs_enforced() -> None:
    policy = SSRFPolicy(extra_blocked_cidrs=["93.184.216.0/24", "64:ff9b::/96"])
    assert policy.ip_block_reason(ipaddress.ip_address("93.184.216.34")) is not None
    # I-22 regression: extra_blocked_cidrs also enforced on NAT64 well-known prefix
    assert policy.ip_block_reason(ipaddress.ip_address("64:ff9b::93.184.216.34")) is not None
    assert policy.ip_block_reason(ipaddress.ip_address("1.1.1.1")) is None


def test_validate_url_and_literal_ip_without_dns() -> None:
    policy = SSRFPolicy()
    assert policy.validate_url_and_literal_ip("https://example.com/rss") == (
        "https",
        "example.com",
        443,
    )
    assert policy.validate_url_and_literal_ip("http://93.184.216.34/rss") == (
        "http",
        "93.184.216.34",
        80,
    )
    with pytest.raises(SSRFBlockedError):
        policy.validate_url_and_literal_ip("http://192.168.1.1/rss")
    with pytest.raises(SSRFBlockedError):
        policy.validate_url_and_literal_ip("http://[::1]/rss")


async def test_resolve_and_validate_dual_homed_fail_closed() -> None:
    async def dual_resolver(_host: str, _port: int) -> list[ipaddress.IPv4Address]:
        return [ipaddress.IPv4Address("93.184.216.34"), ipaddress.IPv4Address("127.0.0.1")]

    policy = SSRFPolicy(resolver=dual_resolver)
    with pytest.raises(SSRFBlockedError):
        await policy.resolve_and_validate("dual.example.com", 443)


async def test_resolve_and_validate_empty_dns_raises_connection_error() -> None:
    async def empty_resolver(_host: str, _port: int) -> list[ipaddress.IPv4Address]:
        return []

    policy = SSRFPolicy(resolver=empty_resolver)
    with pytest.raises(SSRFConnectionError):
        await policy.resolve_and_validate("nx.example.com", 443)


async def test_default_dns_resolver_dedups_and_handles_scope_and_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import socket

    from smartrss.security.safe_http import default_dns_resolver

    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(
        host: str, port: int, **kwargs: object
    ) -> list[tuple[int, int, int, str, tuple[object, ...]]]:
        if host == "fail.example.com":
            raise socket.gaierror("NXDOMAIN")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1%eth0", port, 0, 1)),
        ]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    ips = await default_dns_resolver("ok.example.com", 443)
    assert ips == [ipaddress.IPv4Address("93.184.216.34"), ipaddress.IPv6Address("fe80::1")]

    with pytest.raises(SSRFConnectionError):
        await default_dns_resolver("fail.example.com", 443)

    # Also exercise multicast/reserved/unspecified IPv4+IPv6 branches explicitly
    policy = SSRFPolicy()
    assert policy.ip_block_reason(ipaddress.ip_address("240.0.0.1")) is not None
    assert policy.ip_block_reason(ipaddress.ip_address("224.0.0.251")) is not None
    assert policy.ip_block_reason(ipaddress.ip_address("::")) is not None
