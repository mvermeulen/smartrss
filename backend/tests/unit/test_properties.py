"""Hypothesis property-based tests for URL canonicalization, backoff, and SSRF IP rules (I-07)."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from smartrss.domain.backoff import (
    MAX_BACKOFF_SECONDS,
    MAX_JOB_RETRY_SECONDS,
    apply_jitter,
    compute_error_backoff,
    compute_job_retry_delay,
)
from smartrss.domain.url_canon import canonicalize_url
from smartrss.security.safe_http import SSRFPolicy

_HOST_LABELS = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=10)
_PATH_SEG = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=12)
_QUERY_KEY = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8).filter(
    lambda k: (
        not k.startswith(("utm_", "mtm_", "pk_"))
        and k not in {"ref", "source", "si", "spm", "fbclid", "gclid", "dclid"}
    )
)
_QUERY_VAL = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=0, max_size=8)


@settings(max_examples=80)
@given(
    scheme=st.sampled_from(["http", "https", "HTTP", "HTTPS"]),
    host=_HOST_LABELS,
    tld=st.sampled_from(["com", "org", "net"]),
    segs=st.lists(_PATH_SEG, min_size=0, max_size=3),
    params=st.lists(st.tuples(_QUERY_KEY, _QUERY_VAL), min_size=0, max_size=4),
    utm_val=_QUERY_VAL,
    fragment=_PATH_SEG,
)
def test_canonicalize_url_idempotent_and_strips_tracking_and_fragment(
    scheme: str,
    host: str,
    tld: str,
    segs: list[str],
    params: list[tuple[str, str]],
    utm_val: str,
    fragment: str,
) -> None:
    path = "/" + "/".join(segs)
    base_q = "&".join(f"{k}={v}" for k, v in params)
    tracked_q = f"{base_q}&utm_source={utm_val}&fbclid=123" if base_q else f"utm_source={utm_val}"

    u_clean = (
        f"{scheme}://{host}.{tld}{path}?{base_q}" if base_q else f"{scheme}://{host}.{tld}{path}"
    )
    u_tracked = f"{scheme}://{host}.{tld}{path}?{tracked_q}#{fragment}"

    c1 = canonicalize_url(u_tracked)
    c2 = canonicalize_url(c1)
    assert c1 == c2  # Idempotent
    assert c1 == canonicalize_url(u_clean)  # Tracking params & fragment invariant
    assert "#" not in c1


@settings(max_examples=80)
@given(
    interval=st.integers(min_value=5, max_value=1440),
    errors=st.integers(min_value=1, max_value=20),
)
def test_backoff_bounds_and_monotonicity(interval: int, errors: int) -> None:
    d1 = compute_error_backoff(interval, errors, jitter=False)
    d2 = compute_error_backoff(interval, errors + 1, jitter=False)
    assert d1 <= d2
    assert d2.total_seconds() <= MAX_BACKOFF_SECONDS

    j = apply_jitter(float(d1.total_seconds()), ratio=0.10)
    assert 1.0 <= j <= d1.total_seconds() * 1.10 + 1e-6

    jr = compute_job_retry_delay(errors)
    assert 60 <= jr.total_seconds() <= MAX_JOB_RETRY_SECONDS


@settings(max_examples=100)
@given(
    net=st.sampled_from(
        [
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
            ipaddress.ip_network("127.0.0.0/8"),
            ipaddress.ip_network("169.254.0.0/16"),
            ipaddress.ip_network("100.64.0.0/10"),
        ]
    ),
    offset=st.integers(min_value=0, max_value=65535),
)
def test_ssrf_policy_always_blocks_private_ipv4_and_transition_wrappers(
    net: ipaddress.IPv4Network,
    offset: int,
) -> None:
    policy = SSRFPolicy()
    addr_int = int(net.network_address) + (offset % net.num_addresses)
    v4 = ipaddress.IPv4Address(addr_int)
    assert policy.ip_block_reason(v4) is not None

    # IPv4-mapped IPv6
    mapped = ipaddress.IPv6Address(f"::ffff:{v4}")
    assert policy.ip_block_reason(mapped) is not None

    # NAT64 well-known prefix
    nat64 = ipaddress.IPv6Address(f"64:ff9b::{v4}")
    assert policy.ip_block_reason(nat64) is not None

    # 6to4 prefix
    octets = v4.packed
    sixtofour = ipaddress.IPv6Address(
        f"2002:{octets[0]:02x}{octets[1]:02x}:{octets[2]:02x}{octets[3]:02x}::1"
    )
    assert policy.ip_block_reason(sixtofour) is not None


def test_static_guard_feedparser_parse_always_uses_bytesio() -> None:
    """I-01 regression guard: forbid calling feedparser.parse() without io.BytesIO(...)."""
    src_root = Path(__file__).resolve().parents[2] / "src" / "smartrss"
    pattern = re.compile(r"feedparser\.parse\(([^)]+)\)")
    matches_found = 0
    for py_file in src_root.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        for match in pattern.finditer(content):
            matches_found += 1
            first_arg = match.group(1).split(",", 1)[0].strip()
            assert first_arg.startswith("io.BytesIO("), (
                f"{py_file}: feedparser.parse() must wrap untrusted input in io.BytesIO(), "
                f"got {first_arg!r}"
            )
    assert matches_found >= 1
