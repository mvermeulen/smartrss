"""Unit tests for pairing codes, bearer hashing, rate limiting, config, and JSON logging."""

from __future__ import annotations

import json
import logging

import pytest

from smartrss.config import Settings
from smartrss.logging import JsonLogFormatter
from smartrss.security.auth import (
    GlobalSlidingWindowRateLimiter,
    InvalidPairingCodeError,
    generate_bearer_token,
    generate_pairing_code,
    hash_secret,
    normalize_pairing_code,
)


def test_pairing_code_generation_and_normalization() -> None:
    code = generate_pairing_code()
    parts = code.split("-")
    assert len(parts) == 4
    assert all(len(p) == 4 for p in parts)

    norm = normalize_pairing_code(code)
    assert len(norm) == 16
    assert normalize_pairing_code(f"  {code.lower().replace('-', ' ')} ") == norm

    # Crockford confusable mapping: O->0, I/L->1
    assert normalize_pairing_code("OOOO-IIII-LLLL-0000") == "0000111111110000"


def test_normalize_pairing_code_rejects_malformed() -> None:
    for bad in ("", "SHORT", "AAAA-BBBB-CCCC-UUU!", "AAAA-BBBB-CCCC-DDDD-EEEE"):
        with pytest.raises(InvalidPairingCodeError):
            normalize_pairing_code(bad)


def test_bearer_token_and_hash() -> None:
    t1 = generate_bearer_token()
    t2 = generate_bearer_token()
    assert t1 != t2
    assert len(t1) >= 40
    assert len(hash_secret(t1)) == 64


async def test_global_sliding_window_rate_limiter() -> None:
    now = [1000.0]
    limiter = GlobalSlidingWindowRateLimiter(
        max_attempts=5, window_seconds=300.0, clock=lambda: now[0]
    )

    for _ in range(5):
        allowed, retry_after = await limiter.check_and_record()
        assert allowed is True
        assert retry_after == 0

    allowed, retry_after = await limiter.check_and_record()
    assert allowed is False
    assert retry_after == 300

    # Advance past window
    now[0] += 301.0
    allowed, retry_after = await limiter.check_and_record()
    assert allowed is True
    assert retry_after == 0


def test_settings_rejects_wildcards() -> None:
    with pytest.raises(ValueError):
        Settings(CORS_ALLOW_ORIGINS="*")
    with pytest.raises(ValueError):
        Settings(ALLOWED_HOSTS="*.example.com")

    cfg = Settings(
        ALLOWED_HOSTS="localhost, smartrss.lan:8080",
        CORS_ALLOW_ORIGINS="http://localhost:8080",
        SSRF_BLOCKED_CIDRS="198.51.100.0/24",
    )
    assert cfg.allowed_hosts == frozenset({"localhost", "smartrss.lan:8080"})
    assert cfg.cors_allow_origins == ["http://localhost:8080"]
    assert cfg.ssrf_blocked_cidrs == ["198.51.100.0/24"]


def test_json_log_formatter() -> None:
    fmt = JsonLogFormatter()
    rec = logging.LogRecord(
        name="smartrss.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    rec.job_id = 42
    parsed = json.loads(fmt.format(rec))
    assert parsed["level"] == "WARNING"
    assert parsed["msg"] == "hello world"
    assert parsed["job_id"] == 42
