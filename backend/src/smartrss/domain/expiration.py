"""Pure unread expiration TTL and timestamp clamping logic (data-model.md §6.1)."""

from __future__ import annotations

from datetime import datetime, timedelta


def effective_ttl_days(
    *,
    is_starred: bool,
    feed_override: int | None,
    global_default: int,
) -> int | None:
    """Return effective TTL in days, or None when the item must never expire."""
    if is_starred:
        return None
    if feed_override is not None:
        return None if feed_override == 0 else feed_override
    return None if global_default == 0 else global_default


def should_expire(
    *,
    state: str,
    is_starred: bool,
    fetched_at: datetime,
    unexpired_at: datetime | None,
    feed_override: int | None,
    global_default: int,
    now: datetime,
) -> bool:
    """Return True if an item satisfies the exact unread expiration formula."""
    if state != "unread":
        return False
    ttl = effective_ttl_days(
        is_starred=is_starred,
        feed_override=feed_override,
        global_default=global_default,
    )
    if ttl is None:
        return False
    age_anchor = unexpired_at if unexpired_at is not None else fetched_at
    return age_anchor < (now - timedelta(days=ttl))


def clamp_published_at(published_at: datetime | None, fetched_at: datetime) -> datetime | None:
    """Clamp future-dated published_at to fetched_at."""
    if published_at is None:
        return None
    return min(published_at, fetched_at)
