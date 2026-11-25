"""Unit tests for HTTP polling backoff, Retry-After, Cache-Control, and jitter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from smartrss.domain.backoff import (
    MAX_BACKOFF_SECONDS,
    MIN_RETRY_AFTER_SECONDS,
    apply_jitter,
    compute_429_backoff,
    compute_error_backoff,
    compute_job_retry_delay,
    compute_next_poll_delay,
    parse_cache_control_max_age,
    parse_retry_after,
)


def test_parse_retry_after_delta_seconds_and_clamping() -> None:
    now = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("120", now=now) == 120
    assert parse_retry_after("5", now=now) == MIN_RETRY_AFTER_SECONDS
    assert parse_retry_after("999999", now=now) == MAX_BACKOFF_SECONDS
    assert parse_retry_after("invalid", now=now) is None
    assert parse_retry_after(None, now=now) is None
    assert parse_retry_after("   ", now=now) is None


def test_parse_retry_after_http_date() -> None:
    now = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("Wed, 25 Nov 2026 12:10:00 GMT", now=now) == 600
    assert parse_retry_after("Wed, 25 Nov 2026 11:00:00 GMT", now=now) == MIN_RETRY_AFTER_SECONDS


def test_parse_cache_control_max_age() -> None:
    assert parse_cache_control_max_age("public, max-age=7200") == 7200
    assert parse_cache_control_max_age("no-cache") is None
    assert parse_cache_control_max_age("max-age=9999999") == MAX_BACKOFF_SECONDS


def test_apply_jitter_bounds() -> None:
    assert apply_jitter(100.0, ratio=0.10, rng=lambda lo, _hi: lo) == pytest.approx(90.0)
    assert apply_jitter(100.0, ratio=0.10, rng=lambda _lo, hi: hi) == pytest.approx(110.0)


def test_compute_next_poll_delay_respects_cache_control_floor() -> None:
    d_default = compute_next_poll_delay(60, cache_control="max-age=120", jitter=False)
    assert d_default == timedelta(minutes=60)

    d_floor = compute_next_poll_delay(60, cache_control="public, max-age=7200", jitter=False)
    assert d_floor == timedelta(seconds=7200)

    d_jitter = compute_next_poll_delay(60, jitter=True, rng=lambda _lo, hi: hi)
    assert d_jitter == timedelta(seconds=3600 * 1.1)


def test_compute_error_and_429_and_job_backoff() -> None:
    assert compute_error_backoff(60, 1, jitter=False) == timedelta(hours=1)
    assert compute_error_backoff(60, 2, jitter=False) == timedelta(hours=2)
    assert compute_error_backoff(60, 10, jitter=False) == timedelta(hours=24)

    now = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
    assert compute_429_backoff("300", 60, 1, now=now, jitter=False) == timedelta(seconds=300)
    assert compute_429_backoff(None, 60, 2, now=now, jitter=False) == timedelta(hours=2)

    assert compute_job_retry_delay(1) == timedelta(seconds=60)
    assert compute_job_retry_delay(2) == timedelta(seconds=120)
    assert compute_job_retry_delay(10) == timedelta(seconds=3600)
