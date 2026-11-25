"""Polite RSS/Atom feed polling stage (poll_feed, §9.1)."""

from __future__ import annotations

import asyncio
import calendar
import io
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import feedparser
import httpx
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smartrss.config import get_settings
from smartrss.db.models import AppSettings, Feed, Item
from smartrss.db.sync_log import SyncEvent, execute_synced, sync_log
from smartrss.domain.backoff import (
    compute_429_backoff,
    compute_error_backoff,
    compute_next_poll_delay,
)
from smartrss.domain.expiration import clamp_published_at
from smartrss.domain.url_canon import canonicalize_url, compute_text_hash, derive_fallback_guid
from smartrss.jobs.queue import enqueue_job
from smartrss.search.fts import SearchVectorInput, rebuild_items_search_vectors_batch
from smartrss.security.safe_http import FetchResult, SafeHttpClient, SafeHttpError

logger = logging.getLogger(__name__)

MAX_FEED_BYTES = 5_000_000
BROKEN_ERROR_THRESHOLD = 5
UPDATE_WINDOW_DAYS = 7
INSERT_CHUNK_SIZE = 500  # 500 * 24 cols = 12,000 params, well under Postgres 65,535 cap (I-05)


@dataclass(frozen=True, slots=True)
class ParsedEntry:
    guid: str
    raw_url: str
    canonical_url: str
    title: str
    author: str | None
    published_at: datetime | None
    raw_summary: str | None
    entry_hash: str


@dataclass(frozen=True, slots=True)
class ParsedFeedResult:
    feed_title: str | None
    feed_site_url: str | None
    entries: list[ParsedEntry]
    skipped_count: int


@dataclass(frozen=True, slots=True)
class PollFeedOutcome:
    metric_status: str  # "ok" | "failed" | "skipped"
    items_ingested: int = 0
    items_updated: int = 0
    repoll_needed: bool = False


class PollFeedUnexpectedError(Exception):
    """Unexpected poll_feed failure, tagged with the feed_url the poll actually fetched."""

    def __init__(self, feed_url: str, cause: BaseException) -> None:
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.feed_url = feed_url
        self.cause = cause


def _parse_struct_time(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        ts = calendar.timegm(value)
        return datetime.fromtimestamp(ts, tz=UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _extract_entry_summary(entry: Any) -> str | None:
    summary = str(entry.get("summary") or "").strip()
    content_list = entry.get("content")
    content_val = ""
    if isinstance(content_list, list) and content_list:
        first = content_list[0]
        if isinstance(first, dict):
            content_val = str(first.get("value") or "").strip()
    if content_val and len(content_val) > len(summary):
        return content_val
    return summary or None


def _parse_feed_bytes(
    body_bytes: bytes,
    *,
    fetched_at: datetime,
    base_url: str,
    content_type: str | None = None,
) -> ParsedFeedResult:
    """Parse feed bytes via io.BytesIO with HTTP context (I-01, I-06).

    NEVER pass raw bytes/str directly to feedparser.parse(), which interprets paths
    as local filesystem files.
    """
    response_headers: dict[str, str] = {"content-location": base_url}
    if content_type:
        response_headers["content-type"] = content_type

    parsed = feedparser.parse(io.BytesIO(body_bytes), response_headers=response_headers)
    feed_info = parsed.get("feed") or {}
    entries = parsed.get("entries") or []

    if parsed.get("bozo") and not entries and not feed_info.get("title"):
        bozo_exc = parsed.get("bozo_exception")
        raise ValueError(f"Malformed feed XML: {bozo_exc}")

    feed_title = str(feed_info.get("title") or "").strip() or None
    raw_site_url = str(feed_info.get("link") or "").strip()
    feed_site_url: str | None = None
    if raw_site_url:
        try:
            canonicalize_url(raw_site_url)
            feed_site_url = raw_site_url
        except ValueError:
            feed_site_url = None

    deduped_entries: list[ParsedEntry] = []
    seen_guids: set[str] = set()
    skipped_count = 0

    for entry in entries:
        raw_url = str(entry.get("link") or "").strip()
        raw_title = str(entry.get("title") or "").strip()
        if not raw_url:
            skipped_count += 1
            continue
        try:
            canon_url = canonicalize_url(raw_url)
        except ValueError:
            skipped_count += 1
            continue

        title = raw_title or raw_url
        raw_id = str(entry.get("id") or "").strip()
        guid = raw_id if raw_id else derive_fallback_guid(raw_url, title)

        # Intra-batch deduplication by guid (keep first occurrence in feed order)
        if guid in seen_guids:
            continue
        seen_guids.add(guid)

        raw_pub = _parse_struct_time(entry.get("published_parsed")) or _parse_struct_time(
            entry.get("updated_parsed")
        )
        published_at = clamp_published_at(raw_pub, fetched_at)
        raw_summary = _extract_entry_summary(entry)
        author = str(entry.get("author") or "").strip() or None
        entry_hash = compute_text_hash(title, raw_summary)

        deduped_entries.append(
            ParsedEntry(
                guid=guid,
                raw_url=raw_url,
                canonical_url=canon_url,
                title=title,
                author=author,
                published_at=published_at,
                raw_summary=raw_summary,
                entry_hash=entry_hash,
            )
        )

    return ParsedFeedResult(
        feed_title=feed_title,
        feed_site_url=feed_site_url,
        entries=deduped_entries,
        skipped_count=skipped_count,
    )


async def _record_feed_poll_error(
    session: AsyncSession,
    *,
    feed_id: uuid.UUID,
    error_message: str,
    now: datetime,
    expected_feed_url: str | None = None,
    is_429: bool = False,
    retry_after: str | None = None,
    force_broken: bool = False,
) -> bool:
    """Re-read feed FOR UPDATE, apply column-scoped error/backoff update, emit diff-gated event.

    Returns True if feed_url changed concurrently (caller should request a repoll, I-04).
    """
    row = (
        await session.execute(
            sa.select(
                Feed.feed_url,
                Feed.status,
                Feed.health_state,
                Feed.consecutive_errors,
                Feed.last_error,
                Feed.poll_interval_minutes,
            )
            .where(Feed.id == feed_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        return False

    if expected_feed_url is not None and row.feed_url != expected_feed_url:
        # User edited feed_url while this fetch was in flight; do not degrade the new URL (I-04)
        await execute_synced(
            session,
            sa.update(Feed).where(Feed.id == feed_id).values(last_fetched_at=now, updated_at=now),
            lambda _res: [],
        )
        return True

    if row.status in ("paused", "unsubscribed"):
        await execute_synced(
            session,
            sa.update(Feed).where(Feed.id == feed_id).values(last_fetched_at=now, updated_at=now),
            lambda _res: [],
        )
        return False

    new_errors = row.consecutive_errors + 1
    new_health = "broken" if (force_broken or new_errors >= BROKEN_ERROR_THRESHOLD) else "degraded"
    truncated_err = error_message[:1000]

    if is_429:
        delay = compute_429_backoff(retry_after, row.poll_interval_minutes, new_errors, now=now)
    else:
        delay = compute_error_backoff(row.poll_interval_minutes, new_errors)

    synced_changed = (
        new_health != row.health_state
        or new_errors != row.consecutive_errors
        or truncated_err != row.last_error
    )

    _, events = await execute_synced(
        session,
        sa.update(Feed)
        .where(Feed.id == feed_id)
        .values(
            health_state=new_health,
            consecutive_errors=new_errors,
            last_error=truncated_err,
            last_fetched_at=now,
            next_fetch_at=now + delay,
            updated_at=now,
        ),
        lambda _res: [SyncEvent("feed", feed_id, "upsert")] if synced_changed else [],
    )
    if events:
        await sync_log.record(session, events)
    return False


async def record_poll_feed_unexpected_failure(
    session_factory: async_sessionmaker[AsyncSession],
    feed_id: uuid.UUID,
    error_message: str,
    *,
    expected_feed_url: str | None = None,
) -> bool:
    """Fresh-transaction failure hook when poll_feed raises unexpectedly or dead-letters (§8.1).

    When ``expected_feed_url`` is given and no longer matches the row (concurrent PATCH),
    the new URL's health is left untouched and True is returned so the caller repolls (I-04).
    """
    async with session_factory() as session:
        repoll = await _record_feed_poll_error(
            session,
            feed_id=feed_id,
            error_message=error_message,
            now=datetime.now(UTC),
            expected_feed_url=expected_feed_url,
        )
        await session.commit()
    return repoll


async def run_poll_feed(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: SafeHttpClient,
    *,
    feed_id: uuid.UUID,
    run_id: uuid.UUID | None = None,
) -> PollFeedOutcome:
    """Execute the poll_feed stage for a single feed."""
    # 1. Pre-check (read-only)
    async with session_factory() as session:
        pre = (
            await session.execute(
                sa.select(
                    Feed.id,
                    Feed.feed_url,
                    Feed.status,
                    Feed.health_state,
                    Feed.etag,
                    Feed.last_modified,
                    Feed.poll_interval_minutes,
                ).where(Feed.id == feed_id)
            )
        ).one_or_none()

    if pre is None or pre.status in ("paused", "unsubscribed") or pre.health_state == "gone":
        return PollFeedOutcome(metric_status="skipped")

    try:
        return await _poll_after_precheck(
            session_factory, http_client, feed_id=feed_id, run_id=run_id, pre=pre
        )
    except Exception as exc:
        # Carry the URL this poll actually fetched so the failure hook never degrades
        # a feed_url the user edited while this poll was in flight (I-04).
        raise PollFeedUnexpectedError(pre.feed_url, exc) from exc


async def _poll_after_precheck(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: SafeHttpClient,
    *,
    feed_id: uuid.UUID,
    run_id: uuid.UUID | None,
    pre: Any,
) -> PollFeedOutcome:
    """Fetch, parse, and persist steps of poll_feed (after the read-only pre-check)."""
    # 2. HTTP Fetch (outside any DB write transaction)
    cond_headers: dict[str, str] = {}
    if pre.etag:
        cond_headers["If-None-Match"] = pre.etag
    if pre.last_modified:
        cond_headers["If-Modified-Since"] = pre.last_modified

    fetched_at = datetime.now(UTC)
    try:
        fetch_result: FetchResult = await http_client.fetch(
            pre.feed_url,
            headers=cond_headers,
            max_bytes=MAX_FEED_BYTES,
        )
    except (SafeHttpError, httpx.HTTPError, TimeoutError, OSError) as exc:
        async with session_factory() as session:
            repoll = await _record_feed_poll_error(
                session,
                feed_id=feed_id,
                error_message=f"{type(exc).__name__}: {exc}",
                now=fetched_at,
                expected_feed_url=pre.feed_url,
            )
            await session.commit()
        return PollFeedOutcome(
            metric_status="skipped" if repoll else "failed",
            repoll_needed=repoll,
        )

    # Check permanent redirect target (301/308 chain)
    new_feed_url: str | None = None
    if fetch_result.permanent_redirect_url and fetch_result.permanent_redirect_url != pre.feed_url:
        new_feed_url = fetch_result.permanent_redirect_url

    # Non-200 handling
    status_code = fetch_result.status_code
    if status_code == 304:
        async with session_factory() as session:
            row = (
                await session.execute(
                    sa.select(
                        Feed.status,
                        Feed.health_state,
                        Feed.consecutive_errors,
                        Feed.last_error,
                        Feed.feed_url,
                        Feed.poll_interval_minutes,
                    )
                    .where(Feed.id == feed_id)
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                await session.commit()
                return PollFeedOutcome(metric_status="skipped")

            if row.feed_url != pre.feed_url:
                await execute_synced(
                    session,
                    sa.update(Feed)
                    .where(Feed.id == feed_id)
                    .values(last_fetched_at=fetched_at, updated_at=fetched_at),
                    lambda _res: [],
                )
                await session.commit()
                return PollFeedOutcome(metric_status="skipped", repoll_needed=True)

            if row.status in ("paused", "unsubscribed"):
                await execute_synced(
                    session,
                    sa.update(Feed)
                    .where(Feed.id == feed_id)
                    .values(last_fetched_at=fetched_at, updated_at=fetched_at),
                    lambda _res: [],
                )
                await session.commit()
                return PollFeedOutcome(metric_status="skipped")

            # Check permanent redirect collision even on 304
            effective_feed_url = row.feed_url
            if new_feed_url and new_feed_url != row.feed_url:
                other_id = (
                    await session.execute(
                        sa.select(Feed.id).where(Feed.feed_url == new_feed_url, Feed.id != feed_id)
                    )
                ).scalar_one_or_none()
                if other_id is not None:
                    await _record_feed_poll_error(
                        session,
                        feed_id=feed_id,
                        error_message=(
                            f"Permanent redirect collides with existing feed {other_id}: "
                            f"{new_feed_url}"
                        ),
                        now=fetched_at,
                        expected_feed_url=pre.feed_url,
                        force_broken=True,
                    )
                    await session.commit()
                    return PollFeedOutcome(metric_status="failed")
                effective_feed_url = new_feed_url

            delay = compute_next_poll_delay(
                row.poll_interval_minutes,
                cache_control=fetch_result.headers.get("cache-control"),
            )
            synced_changed = (
                row.health_state != "healthy"
                or row.consecutive_errors != 0
                or row.last_error is not None
                or effective_feed_url != row.feed_url
            )
            update_vals: dict[str, Any] = {
                "health_state": "healthy",
                "consecutive_errors": 0,
                "last_error": None,
                "last_fetched_at": fetched_at,
                "last_success_at": fetched_at,
                "next_fetch_at": fetched_at + delay,
                "updated_at": fetched_at,
            }
            if effective_feed_url != row.feed_url:
                update_vals["feed_url"] = effective_feed_url
            if fetch_result.headers.get("etag"):
                update_vals["etag"] = fetch_result.headers["etag"]
            if fetch_result.headers.get("last-modified"):
                update_vals["last_modified"] = fetch_result.headers["last-modified"]

            _, events = await execute_synced(
                session,
                sa.update(Feed).where(Feed.id == feed_id).values(**update_vals),
                lambda _res: [SyncEvent("feed", feed_id, "upsert")] if synced_changed else [],
            )
            if events:
                await sync_log.record(session, events)
            await session.commit()
        return PollFeedOutcome(metric_status="ok")

    if status_code == 410:
        async with session_factory() as session:
            row_410 = (
                await session.execute(
                    sa.select(Feed.feed_url, Feed.status, Feed.health_state, Feed.last_error)
                    .where(Feed.id == feed_id)
                    .with_for_update()
                )
            ).one_or_none()
            if row_410 is not None:
                if row_410.feed_url != pre.feed_url:
                    await execute_synced(
                        session,
                        sa.update(Feed)
                        .where(Feed.id == feed_id)
                        .values(last_fetched_at=fetched_at, updated_at=fetched_at),
                        lambda _res: [],
                    )
                    await session.commit()
                    return PollFeedOutcome(metric_status="skipped", repoll_needed=True)

                if row_410.status in ("paused", "unsubscribed"):
                    await execute_synced(
                        session,
                        sa.update(Feed)
                        .where(Feed.id == feed_id)
                        .values(last_fetched_at=fetched_at, updated_at=fetched_at),
                        lambda _res: [],
                    )
                else:
                    err_msg = "HTTP 410 Gone"
                    synced_changed = row_410.health_state != "gone" or row_410.last_error != err_msg
                    _, events = await execute_synced(
                        session,
                        sa.update(Feed)
                        .where(Feed.id == feed_id)
                        .values(
                            health_state="gone",
                            last_error=err_msg,
                            last_fetched_at=fetched_at,
                            next_fetch_at=None,
                            updated_at=fetched_at,
                        ),
                        lambda _res: (
                            [SyncEvent("feed", feed_id, "upsert")] if synced_changed else []
                        ),
                    )
                    if events:
                        await sync_log.record(session, events)
            await session.commit()
        return PollFeedOutcome(metric_status="failed")

    if status_code == 429:
        async with session_factory() as session:
            repoll = await _record_feed_poll_error(
                session,
                feed_id=feed_id,
                error_message="HTTP 429 Too Many Requests",
                now=fetched_at,
                expected_feed_url=pre.feed_url,
                is_429=True,
                retry_after=fetch_result.headers.get("retry-after"),
            )
            await session.commit()
        return PollFeedOutcome(
            metric_status="skipped" if repoll else "failed",
            repoll_needed=repoll,
        )

    if not (200 <= status_code < 300):
        async with session_factory() as session:
            repoll = await _record_feed_poll_error(
                session,
                feed_id=feed_id,
                error_message=f"HTTP {status_code}",
                now=fetched_at,
                expected_feed_url=pre.feed_url,
            )
            await session.commit()
        return PollFeedOutcome(
            metric_status="skipped" if repoll else "failed",
            repoll_needed=repoll,
        )

    # 3. Parse & Normalize off the event loop (I-01, I-06, I-19)
    try:
        parsed_feed = await asyncio.to_thread(
            _parse_feed_bytes,
            fetch_result.body_bytes,
            fetched_at=fetched_at,
            base_url=fetch_result.final_url,
            content_type=fetch_result.content_type,
        )
    except ValueError as exc:
        async with session_factory() as session:
            repoll = await _record_feed_poll_error(
                session,
                feed_id=feed_id,
                error_message=str(exc),
                now=fetched_at,
                expected_feed_url=pre.feed_url,
            )
            await session.commit()
        return PollFeedOutcome(
            metric_status="skipped" if repoll else "failed",
            repoll_needed=repoll,
        )

    feed_title = parsed_feed.feed_title
    feed_site_url = parsed_feed.feed_site_url
    parsed_entries = parsed_feed.entries

    if parsed_feed.skipped_count > 0:
        logger.warning(
            "poll_feed_skipped_entries",
            extra={
                "feed_id": str(feed_id),
                "skipped_count": parsed_feed.skipped_count,
                "parsed_count": len(parsed_entries),
            },
        )
        if not parsed_entries:
            async with session_factory() as session:
                repoll = await _record_feed_poll_error(
                    session,
                    feed_id=feed_id,
                    error_message=(
                        f"All {parsed_feed.skipped_count} feed entries had missing or invalid links"
                    ),
                    now=fetched_at,
                    expected_feed_url=pre.feed_url,
                )
                await session.commit()
            return PollFeedOutcome(
                metric_status="skipped" if repoll else "failed",
                repoll_needed=repoll,
            )

    # 4. Persist Transaction (short DB tx + SyncLogWriter at end)
    items_ingested = 0
    items_updated = 0

    async with session_factory() as session:
        feed_row = (
            await session.execute(
                sa.select(
                    Feed.id,
                    Feed.status,
                    Feed.fetch_full_text,
                    Feed.initial_backfill_done,
                    Feed.poll_interval_minutes,
                    Feed.health_state,
                    Feed.consecutive_errors,
                    Feed.last_error,
                    Feed.title,
                    Feed.site_url,
                    Feed.feed_url,
                    Feed.field_updated_at,
                )
                .where(Feed.id == feed_id)
                .with_for_update()
            )
        ).one_or_none()

        if (
            feed_row is None
            or feed_row.status in ("paused", "unsubscribed")
            or feed_row.health_state == "gone"
        ):
            await session.commit()
            return PollFeedOutcome(metric_status="skipped")

        # I-04: Detect concurrent PATCH feed_url during HTTP I/O
        if feed_row.feed_url != pre.feed_url:
            await execute_synced(
                session,
                sa.update(Feed)
                .where(Feed.id == feed_id)
                .values(last_fetched_at=fetched_at, updated_at=fetched_at),
                lambda _res: [],
            )
            await session.commit()
            return PollFeedOutcome(metric_status="skipped", repoll_needed=True)

        # Check permanent redirect URL collision before applying
        effective_feed_url = feed_row.feed_url
        if new_feed_url and new_feed_url != feed_row.feed_url:
            other_id = (
                await session.execute(
                    sa.select(Feed.id).where(Feed.feed_url == new_feed_url, Feed.id != feed_id)
                )
            ).scalar_one_or_none()
            if other_id is not None:
                await _record_feed_poll_error(
                    session,
                    feed_id=feed_id,
                    error_message=(
                        f"Permanent redirect collides with existing feed {other_id}: {new_feed_url}"
                    ),
                    now=fetched_at,
                    expected_feed_url=pre.feed_url,
                    force_broken=True,
                )
                await session.commit()
                return PollFeedOutcome(metric_status="failed")
            effective_feed_url = new_feed_url

        settings_row = await session.get(AppSettings, 1)
        backfill_cap = settings_row.backfill_cap if settings_row is not None else 10

        all_events: list[SyncEvent] = []

        # Always run dedup lookup first (for both initial/reactivation backfill and steady-state)
        batch_guids = {e.guid for e in parsed_entries}
        existing_by_guid: dict[str, Item] = {}
        if batch_guids:
            guid_list = list(batch_guids)
            for i in range(0, len(guid_list), INSERT_CHUNK_SIZE):
                chunk_guids = guid_list[i : i + INSERT_CHUNK_SIZE]
                guid_rows = (
                    (
                        await session.execute(
                            sa.select(Item).where(
                                Item.feed_id == feed_id, Item.guid.in_(chunk_guids)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for item in guid_rows:
                    existing_by_guid[item.guid] = item

        # Guarded unstable-GUID fallback (N-01)
        batch_url_counts: dict[str, int] = {}
        for entry in parsed_entries:
            batch_url_counts[entry.canonical_url] = batch_url_counts.get(entry.canonical_url, 0) + 1

        unmatched_urls = list(
            {e.canonical_url for e in parsed_entries if e.guid not in existing_by_guid}
        )
        existing_by_url: dict[str, list[Item]] = {}
        for i in range(0, len(unmatched_urls), INSERT_CHUNK_SIZE):
            chunk_urls = unmatched_urls[i : i + INSERT_CHUNK_SIZE]
            url_rows = (
                (
                    await session.execute(
                        sa.select(Item).where(
                            Item.feed_id == feed_id,
                            Item.canonical_url.in_(chunk_urls),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for item in url_rows:
                existing_by_url.setdefault(item.canonical_url, []).append(item)

        matched_existing: list[tuple[Item, ParsedEntry]] = []
        new_entries: list[ParsedEntry] = []

        for entry in parsed_entries:
            existing_item = existing_by_guid.get(entry.guid)
            if existing_item is not None:
                matched_existing.append((existing_item, entry))
                continue

            url_candidates = existing_by_url.get(entry.canonical_url, [])
            if (
                len(url_candidates) == 1
                and batch_url_counts.get(entry.canonical_url, 0) == 1
                and url_candidates[0].guid not in batch_guids
            ):
                matched_existing.append((url_candidates[0], entry))
            else:
                new_entries.append(entry)

        # Handle existing entries in strict item.id ASC order (§5.1, P-04)
        matched_existing.sort(key=lambda pair: pair[0].id)
        update_cutoff = fetched_at - timedelta(days=UPDATE_WINDOW_DAYS)
        fts_entries: list[SearchVectorInput] = []
        for existing_item, entry in matched_existing:
            if (
                existing_item.state == "unread"
                and existing_item.fetched_at >= update_cutoff
                and entry.entry_hash != existing_item.entry_hash
            ):
                next_extract_status = "pending" if feed_row.fetch_full_text else "fallback_rss"
                item_id_for_event = existing_item.id

                def _upd_event(
                    _res: object, _iid: uuid.UUID = item_id_for_event
                ) -> list[SyncEvent]:
                    return [SyncEvent("item", _iid, "upsert")]

                _, upd_events = await execute_synced(
                    session,
                    sa.update(Item)
                    .where(Item.id == item_id_for_event)
                    .values(
                        title=entry.title,
                        raw_summary=entry.raw_summary,
                        entry_hash=entry.entry_hash,
                        content_hash=entry.entry_hash,
                        extraction_status=next_extract_status,
                        updated_at=fetched_at,
                    ),
                    _upd_event,
                )
                all_events.extend(upd_events)
                fts_entries.append(
                    SearchVectorInput(
                        item_id=existing_item.id,
                        title=entry.title,
                        author=existing_item.author,
                        raw_summary=entry.raw_summary,
                        extracted_text=existing_item.extracted_text,
                    )
                )
                items_updated += 1
                if feed_row.fetch_full_text:
                    await enqueue_job(
                        session,
                        job_type="extract_content",
                        dedupe_key=f"item:{existing_item.id}",
                        feed_id=feed_id,
                        item_id=existing_item.id,
                        run_id=run_id,
                        priority=50,
                    )

        # Handle new entries
        muted_state = "read" if feed_row.status == "muted" else "unread"
        muted_read_at = fetched_at if feed_row.status == "muted" else None

        entries_to_insert: list[tuple[ParsedEntry, bool, int]] = []
        if not feed_row.initial_backfill_done:
            # Sort descending by (published_at or fetched_at), preserving feed order on ties
            ordered = sorted(
                new_entries,
                key=lambda e: e.published_at or fetched_at,
                reverse=True,
            )
            for idx, entry in enumerate(ordered):
                is_overflow = idx >= backfill_cap
                entries_to_insert.append((entry, is_overflow, 90))
        else:
            for entry in new_entries:
                entries_to_insert.append((entry, False, 50))

        inserted_ids: list[uuid.UUID] = []
        if entries_to_insert:
            insert_rows: list[dict[str, Any]] = []
            extract_prio_by_id: dict[uuid.UUID, int] = {}
            insert_row_by_id: dict[uuid.UUID, dict[str, Any]] = {}
            for entry, is_overflow, extract_prio in entries_to_insert:
                item_id = uuid.uuid4()
                if is_overflow:
                    state = "expired"
                    read_at = None
                    expired_at: datetime | None = fetched_at
                    extract_status = "fallback_rss"
                else:
                    state = muted_state
                    read_at = muted_read_at
                    expired_at = None
                    extract_status = "pending" if feed_row.fetch_full_text else "fallback_rss"
                    if feed_row.fetch_full_text:
                        extract_prio_by_id[item_id] = extract_prio

                row_dict: dict[str, Any] = {
                    "id": item_id,
                    "feed_id": feed_id,
                    "guid": entry.guid,
                    "raw_url": entry.raw_url,
                    "canonical_url": entry.canonical_url,
                    "title": entry.title,
                    "author": entry.author,
                    "published_at": entry.published_at,
                    "fetched_at": fetched_at,
                    "raw_summary": entry.raw_summary,
                    "extracted_text": None,
                    "extraction_status": extract_status,
                    "entry_hash": entry.entry_hash,
                    "content_hash": entry.entry_hash,
                    "state": state,
                    "is_starred": False,
                    "read_at": read_at,
                    "starred_at": None,
                    "expired_at": expired_at,
                    "read_state_updated_at": fetched_at,
                    "starred_updated_at": fetched_at,
                    "unexpired_at": None,
                    "created_at": fetched_at,
                    "updated_at": fetched_at,
                }
                insert_rows.append(row_dict)
                insert_row_by_id[item_id] = row_dict

            # I-05: Insert in chunks of 500 rows to stay well under Postgres's 65,535 param limit
            for start in range(0, len(insert_rows), INSERT_CHUNK_SIZE):
                chunk_rows = insert_rows[start : start + INSERT_CHUNK_SIZE]
                insert_stmt = (
                    postgresql.insert(Item)
                    .values(chunk_rows)
                    .on_conflict_do_nothing(index_elements=[Item.feed_id, Item.guid])
                    .returning(Item.id)
                )

                def _collect_inserted(res: Any) -> list[SyncEvent]:
                    ids = list(res.scalars().all())
                    inserted_ids.extend(ids)
                    return [SyncEvent("item", iid, "upsert") for iid in ids]

                _, item_events = await execute_synced(session, insert_stmt, _collect_inserted)
                all_events.extend(item_events)

            items_ingested = len(inserted_ids)

            for iid in inserted_ids:
                row_data = insert_row_by_id[iid]
                fts_entries.append(
                    SearchVectorInput(
                        item_id=iid,
                        title=str(row_data["title"]),
                        author=cast(str | None, row_data["author"]),
                        raw_summary=cast(str | None, row_data["raw_summary"]),
                        extracted_text=None,
                    )
                )
                prio = extract_prio_by_id.get(iid)
                if prio is not None:
                    await enqueue_job(
                        session,
                        job_type="extract_content",
                        dedupe_key=f"item:{iid}",
                        feed_id=feed_id,
                        item_id=iid,
                        run_id=run_id,
                        priority=prio,
                    )

        if fts_entries:
            await rebuild_items_search_vectors_batch(
                session,
                fts_entries,
                fts_language=get_settings().fts_language,
            )

        # Column-scoped feed metadata update (poll-owned columns only)
        delay = compute_next_poll_delay(
            feed_row.poll_interval_minutes,
            cache_control=fetch_result.headers.get("cache-control"),
        )
        effective_title = feed_row.title
        if feed_title and "title" not in (feed_row.field_updated_at or {}):
            effective_title = feed_title

        effective_site_url = feed_site_url if feed_site_url else feed_row.site_url

        feed_update_vals: dict[str, Any] = {
            "etag": fetch_result.headers.get("etag"),
            "last_modified": fetch_result.headers.get("last-modified"),
            "last_fetched_at": fetched_at,
            "last_success_at": fetched_at,
            "next_fetch_at": fetched_at + delay,
            "consecutive_errors": 0,
            "health_state": "healthy",
            "last_error": None,
            "initial_backfill_done": True,
            "updated_at": fetched_at,
            "feed_url": effective_feed_url,
            "site_url": effective_site_url,
            "title": effective_title,
        }

        feed_synced_changed = (
            effective_feed_url != feed_row.feed_url
            or effective_site_url != feed_row.site_url
            or effective_title != feed_row.title
            or feed_row.health_state != "healthy"
            or feed_row.consecutive_errors != 0
            or feed_row.last_error is not None
        )

        _, feed_events = await execute_synced(
            session,
            sa.update(Feed).where(Feed.id == feed_id).values(**feed_update_vals),
            lambda _res: [SyncEvent("feed", feed_id, "upsert")] if feed_synced_changed else [],
        )
        all_events = [*feed_events, *all_events]

        if all_events:
            seqs = await sync_log.record(session, all_events)
            if inserted_ids:
                inserted_set = set(inserted_ids)
                stamp_ids: list[uuid.UUID] = []
                stamp_seqs: list[int] = []
                for ev, seq in zip(all_events, seqs, strict=True):
                    if ev.entity_type == "item" and ev.entity_id in inserted_set:
                        stamp_ids.append(ev.entity_id)
                        stamp_seqs.append(seq)
                if stamp_ids:
                    await execute_synced(
                        session,
                        sa.text(
                            """
                            UPDATE items
                            SET ingest_sync_seq = v.seq
                            FROM (
                                SELECT unnest(CAST(:ids AS uuid[])) AS id,
                                       unnest(CAST(:seqs AS bigint[])) AS seq
                            ) AS v
                            WHERE items.id = v.id
                            """
                        ).bindparams(ids=stamp_ids, seqs=stamp_seqs),
                        lambda _res: [],
                    )
        await session.commit()

    return PollFeedOutcome(
        metric_status="ok",
        items_ingested=items_ingested,
        items_updated=items_updated,
    )
