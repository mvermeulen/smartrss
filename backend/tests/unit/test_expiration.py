"""Unit tests for unread item expiration TTL and published_at clamping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from smartrss.domain.expiration import clamp_published_at, effective_ttl_days, should_expire


def test_effective_ttl_days_precedence() -> None:
    assert effective_ttl_days(is_starred=True, feed_override=7, global_default=30) is None
    assert effective_ttl_days(is_starred=False, feed_override=0, global_default=30) is None
    assert effective_ttl_days(is_starred=False, feed_override=14, global_default=30) == 14
    assert effective_ttl_days(is_starred=False, feed_override=None, global_default=30) == 30
    assert effective_ttl_days(is_starred=False, feed_override=None, global_default=0) is None


def test_should_expire_semantics() -> None:
    now = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
    old = now - timedelta(days=31)
    recent = now - timedelta(days=5)

    # Normal expiry
    assert should_expire(
        state="unread",
        is_starred=False,
        fetched_at=old,
        unexpired_at=None,
        feed_override=None,
        global_default=30,
        now=now,
    )
    # Starred immune
    assert not should_expire(
        state="unread",
        is_starred=True,
        fetched_at=old,
        unexpired_at=None,
        feed_override=None,
        global_default=30,
        now=now,
    )
    # Read / expired ignored
    assert not should_expire(
        state="read",
        is_starred=False,
        fetched_at=old,
        unexpired_at=None,
        feed_override=None,
        global_default=30,
        now=now,
    )
    # unexpired_at grace clock reset
    assert not should_expire(
        state="unread",
        is_starred=False,
        fetched_at=old,
        unexpired_at=recent,
        feed_override=None,
        global_default=30,
        now=now,
    )
    # Feed override shorter than global
    assert should_expire(
        state="unread",
        is_starred=False,
        fetched_at=recent,
        unexpired_at=None,
        feed_override=3,
        global_default=30,
        now=now,
    )


def test_clamp_published_at() -> None:
    fetched = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
    past = fetched - timedelta(hours=2)
    future = fetched + timedelta(days=5)

    assert clamp_published_at(None, fetched) is None
    assert clamp_published_at(past, fetched) == past
    assert clamp_published_at(future, fetched) == fetched
