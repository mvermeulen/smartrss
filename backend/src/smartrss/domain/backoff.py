"""Pure HTTP polling interval, Retry-After, Cache-Control, and backoff calculations."""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

MIN_RETRY_AFTER_SECONDS = 60
MAX_BACKOFF_SECONDS = 24 * 3600  # 24 hours
MAX_JOB_RETRY_SECONDS = 3600  # 1 hour

_MAX_AGE_RE = re.compile(r"(?:^|,)\s*max-age\s*=\s*(\d+)", re.IGNORECASE)

JitterRng = Callable[[float, float], float]


def parse_retry_after(header_value: str | None, *, now: datetime) -> int | None:
    """Parse HTTP Retry-After (delta-seconds or RFC 7231 date), clamped to [60s, 24h]."""
    if not header_value:
        return None
    val = header_value.strip()
    if not val:
        return None

    seconds: float | None = None
    if val.isdigit():
        seconds = float(int(val))
    else:
        try:
            dt = parsedate_to_datetime(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            seconds = (dt - now).total_seconds()
        except (TypeError, ValueError, IndexError):
            return None

    clamped = max(MIN_RETRY_AFTER_SECONDS, min(int(seconds), MAX_BACKOFF_SECONDS))
    return clamped


def parse_cache_control_max_age(header_value: str | None) -> int | None:
    """Extract Cache-Control max-age in seconds if present."""
    if not header_value:
        return None
    match = _MAX_AGE_RE.search(header_value)
    if not match:
        return None
    val = int(match.group(1))
    return min(val, MAX_BACKOFF_SECONDS)


def apply_jitter(
    seconds: float,
    *,
    ratio: float = 0.10,
    rng: JitterRng | None = None,
) -> float:
    """Apply +/- ratio uniform jitter to a positive delay in seconds."""
    uniform = rng or random.uniform
    factor = 1.0 + uniform(-ratio, ratio)
    return max(1.0, seconds * factor)


def compute_next_poll_delay(
    poll_interval_minutes: int,
    *,
    cache_control: str | None = None,
    jitter: bool = True,
    rng: JitterRng | None = None,
) -> timedelta:
    """Compute delay until next poll, respecting Cache-Control max-age floor + jitter."""
    base_seconds = float(max(poll_interval_minutes, 5) * 60)
    max_age = parse_cache_control_max_age(cache_control)
    if max_age is not None and max_age > base_seconds:
        base_seconds = float(max_age)
    final_seconds = apply_jitter(base_seconds, rng=rng) if jitter else base_seconds
    return timedelta(seconds=final_seconds)


def compute_error_backoff(
    poll_interval_minutes: int,
    consecutive_errors: int,
    *,
    jitter: bool = True,
    rng: JitterRng | None = None,
) -> timedelta:
    """Compute exponential backoff min(poll_interval * 2^(errors-1), 24h) + jitter."""
    base_seconds = float(max(poll_interval_minutes, 5) * 60)
    exponent = max(consecutive_errors - 1, 0)
    delay_seconds = min(base_seconds * (2**exponent), float(MAX_BACKOFF_SECONDS))
    final_seconds = apply_jitter(delay_seconds, rng=rng) if jitter else delay_seconds
    return timedelta(seconds=final_seconds)


def compute_429_backoff(
    retry_after_header: str | None,
    poll_interval_minutes: int,
    consecutive_errors: int,
    *,
    now: datetime,
    jitter: bool = True,
    rng: JitterRng | None = None,
) -> timedelta:
    """Compute backoff for HTTP 429 using Retry-After when present, else error backoff."""
    retry_seconds = parse_retry_after(retry_after_header, now=now)
    if retry_seconds is not None:
        final_seconds = (
            apply_jitter(float(retry_seconds), rng=rng) if jitter else float(retry_seconds)
        )
        return timedelta(seconds=final_seconds)
    return compute_error_backoff(poll_interval_minutes, consecutive_errors, jitter=jitter, rng=rng)


def compute_job_retry_delay(attempts: int) -> timedelta:
    """Compute job queue retry delay min(60 * 2^(attempts-1), 3600) seconds."""
    exponent = max(attempts - 1, 0)
    return timedelta(seconds=min(60 * (2**exponent), MAX_JOB_RETRY_SECONDS))
