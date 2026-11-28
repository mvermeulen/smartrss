"""Shared transactional feed CRUD and OPML import/export service (1d-ops, D02).

Used by both FastAPI routes (`api/routes/feeds.py`) and the headless CLI (`cli.py`) so
SSRF/syntax validation, category FOR SHARE lock ordering (C8), concurrent category creation
savepoints (O-05), monotonic LWW timestamps (C7), reactivation defaults (D3), SyncLogWriter
(D2), and poll job enqueueing never drift apart.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smartrss.db.models import DEFAULT_RESEARCH_POLICY, AppSettings, Category, Feed
from smartrss.db.sync_log import SyncEvent, sync_log
from smartrss.domain.opml import OpmlParseError, OpmlSubscription, export_opml, parse_opml
from smartrss.domain.sync_merge import (
    apply_feed_status_transition,
    compute_online_lww_timestamp,
    parse_field_timestamp,
)
from smartrss.domain.url_canon import canonicalize_url, default_title_from_url
from smartrss.jobs.queue import enqueue_job
from smartrss.security.safe_http import SafeHttpError, SSRFPolicy, validate_feed_url_syntax

MAX_OPML_BYTES = 2 * 1024 * 1024  # 2 MiB (matches OpmlImportRequest cap, S5)


class FeedValidationError(ValueError):
    """Raised when a feed URL or OPML payload fails syntax, SSRF, or parse validation."""


class FeedCategoryNotFoundError(ValueError):
    """Raised when a referenced category ID or name does not exist or is soft-deleted (C8)."""


class FeedAlreadySubscribedError(ValueError):
    """Raised when an active, paused, or muted feed already owns the target feed_url."""

    def __init__(
        self,
        existing_feed_id: uuid.UUID,
        existing_status: str,
        feed_url: str,
    ) -> None:
        super().__init__(f"Feed URL is already subscribed (status={existing_status})")
        self.existing_feed_id = existing_feed_id
        self.existing_status = existing_status
        self.feed_url = feed_url


@dataclass(frozen=True, slots=True)
class OpmlImportSummary:
    imported: int
    reactivated: int
    skipped_duplicates: int
    rejected_invalid: int


def stamp_online_lww(
    field_ts: dict[str, str],
    field: str,
    db_now: datetime,
    fallback: datetime,
) -> None:
    """Stamp an LWW field monotonically using DB clock (P-02, C5, C7)."""
    stored_ts = parse_field_timestamp(field_ts, field, fallback) if field_ts else None
    if field not in field_ts and not (
        field.startswith("research_policy.") and "research_policy" in field_ts
    ):
        stored_ts = None
    field_ts[field] = compute_online_lww_timestamp(db_now, stored_ts).isoformat()


def stamp_research_policy_fields(
    field_ts: dict[str, str],
    old_rp: dict[str, Any] | None,
    new_rp: dict[str, Any],
    db_now: datetime,
    fallback: datetime,
) -> None:
    """Stamp granular research_policy.* LWW keys on online updates (S-04, P-02)."""
    prev = old_rp or {}
    for subkey in ("enabled", "fetch_pdfs"):
        if prev.get(subkey) != new_rp.get(subkey) or f"research_policy.{subkey}" not in field_ts:
            stamp_online_lww(field_ts, f"research_policy.{subkey}", db_now, fallback)
    if (
        prev.get("guidance") != new_rp.get("guidance")
        or prev.get("max_hops") != new_rp.get("max_hops")
        or "research_policy" not in field_ts
    ):
        stamp_online_lww(field_ts, "research_policy", db_now, fallback)


def sanitize_site_url(raw: str | None) -> str | None:
    """Keep site_url only if it is a valid http(s) URL; otherwise return None (I-11, S4)."""
    if not raw or not raw.strip():
        return None
    cleaned = raw.strip()
    try:
        canonicalize_url(cleaned)
    except ValueError:
        return None
    return cleaned


async def validate_feed_url(policy: SSRFPolicy, url: str) -> str:
    """Validate feed URL syntax (E-10-F1) + SSRF policy + DNS resolution outside DB locks (C1)."""
    cleaned = url.strip()
    try:
        validate_feed_url_syntax(cleaned)
        _scheme, host, port = policy.validate_url(cleaned)
        await policy.resolve_and_validate(host, port)
    except (SafeHttpError, ValueError) as exc:
        raise FeedValidationError(f"Invalid or blocked feed_url: {exc}") from exc
    return cleaned


async def _get_default_poll_interval(session: AsyncSession) -> int:
    row = await session.get(AppSettings, 1)
    return row.default_poll_interval_minutes if row is not None else 60


async def _get_or_create_category_by_name(
    session: AsyncSession,
    name: str,
    *,
    now: datetime,
    cats_by_lower: dict[str, Category],
    events: list[SyncEvent],
) -> Category:
    """Return active category matching name (case-insensitive) or insert inside a savepoint (O-05).

    Guards against concurrent inserts hitting `uq_categories_active_name` by catching
    `IntegrityError` on the savepoint and re-selecting the winning active row `FOR SHARE`.
    """
    cleaned_name = name.strip()
    cat_key = cleaned_name.lower()
    existing = cats_by_lower.get(cat_key)
    if existing is not None:
        return existing

    try:
        async with session.begin_nested():
            cat = Category(
                id=uuid.uuid4(),
                name=cleaned_name,
                sort_order=0,
                created_at=now,
                updated_at=now,
            )
            session.add(cat)
            await session.flush()
    except IntegrityError:
        winner = (
            await session.execute(
                sa.select(Category)
                .where(
                    sa.func.lower(Category.name) == cat_key,
                    Category.deleted_at.is_(None),
                )
                .with_for_update(read=True)
            )
        ).scalar_one_or_none()
        if winner is None:
            raise
        cats_by_lower[cat_key] = winner
        return winner

    cats_by_lower[cat_key] = cat
    events.append(SyncEvent("category", cat.id, "upsert"))
    return cat


async def list_feeds_query(
    session: AsyncSession,
    *,
    status_filter: str | None = None,
    health_state: str | None = None,
) -> list[Feed]:
    """Return feeds ordered by title ASC, created_at ASC with optional status/health filters."""
    stmt = sa.select(Feed)
    if status_filter is not None:
        stmt = stmt.where(Feed.status == status_filter)
    if health_state is not None:
        stmt = stmt.where(Feed.health_state == health_state)
    stmt = stmt.order_by(Feed.title.asc(), Feed.created_at.asc())
    return list((await session.execute(stmt)).scalars().all())


def _reactivate_or_raise(
    existing: Feed,
    *,
    now: datetime,
    user_title: str | None,
    resolved_category_id: uuid.UUID | None,
    poll_interval_minutes: int | None,
    expiration_days_override: int | None,
    fetch_full_text: bool,
    research_policy: dict[str, Any] | None,
    curation_instructions: str,
    fields_set: Collection[str],
) -> Feed:
    """Reactivate an unsubscribed feed preserving unspecified fields (D3), or raise conflict."""
    if existing.status != "unsubscribed":
        raise FeedAlreadySubscribedError(existing.id, existing.status, existing.feed_url)

    field_ts = dict(existing.field_updated_at or {})
    apply_feed_status_transition(existing, "active", now)
    stamp_online_lww(field_ts, "status", now, existing.created_at)
    if user_title is not None:
        existing.title = user_title
        stamp_online_lww(field_ts, "title", now, existing.created_at)
    if resolved_category_id is not None:
        existing.category_id = resolved_category_id
        stamp_online_lww(field_ts, "category_id", now, existing.created_at)
    if poll_interval_minutes is not None:
        existing.poll_interval_minutes = poll_interval_minutes
        stamp_online_lww(field_ts, "poll_interval_minutes", now, existing.created_at)
    if expiration_days_override is not None:
        existing.expiration_days_override = expiration_days_override
        stamp_online_lww(field_ts, "expiration_days_override", now, existing.created_at)
    if "fetch_full_text" in fields_set:
        existing.fetch_full_text = fetch_full_text
        stamp_online_lww(field_ts, "fetch_full_text", now, existing.created_at)
    if research_policy is not None:
        new_rp = dict(research_policy)
        stamp_research_policy_fields(
            field_ts, existing.research_policy, new_rp, now, existing.created_at
        )
        existing.research_policy = new_rp
    if curation_instructions and curation_instructions != existing.curation_instructions:
        existing.curation_instructions = curation_instructions
        existing.config_version += 1
    existing.field_updated_at = field_ts
    existing.updated_at = now
    return existing


async def create_or_reactivate_feed(
    session: AsyncSession,
    ssrf_policy: SSRFPolicy,
    *,
    feed_url: str,
    title: str | None = None,
    category_id: uuid.UUID | None = None,
    category_name: str | None = None,
    create_category_if_missing: bool = False,
    poll_interval_minutes: int | None = None,
    expiration_days_override: int | None = None,
    fetch_full_text: bool = True,
    research_policy: dict[str, Any] | None = None,
    curation_instructions: str = "",
    fields_set: Collection[str] = (),
) -> tuple[Feed, bool]:
    """Create a new feed or reactivate an unsubscribed feed. Caller owns session.commit() (C6).

    Returns `(feed, reactivated)` where `reactivated` is True when an existing `unsubscribed`
    row was reactivated and False when a new row was inserted.
    """
    if category_id is not None and category_name is not None:
        raise FeedValidationError("Pass either category_id or category_name, not both")

    # 1. Validate URL + DNS strictly BEFORE acquiring any DB row locks (C1, E-10-F1)
    cleaned_url = await validate_feed_url(ssrf_policy, feed_url)

    now: datetime = (await session.execute(sa.select(sa.func.now()))).scalar_one()
    now_iso = now.isoformat()
    events: list[SyncEvent] = []

    # 2. Resolve & lock target category FOR SHARE *before* locking Feed FOR UPDATE (C8 order)
    resolved_category_id: uuid.UUID | None = None
    if category_id is not None:
        cat = (
            await session.execute(
                sa.select(Category.id)
                .where(Category.id == category_id, Category.deleted_at.is_(None))
                .with_for_update(read=True)
            )
        ).scalar_one_or_none()
        if cat is None:
            raise FeedCategoryNotFoundError("Category not found")
        resolved_category_id = cat
    elif category_name is not None:
        cleaned_cat = category_name.strip()
        if not cleaned_cat:
            raise FeedCategoryNotFoundError("Category name cannot be empty")
        cat_rows = (
            (
                await session.execute(
                    sa.select(Category)
                    .where(Category.deleted_at.is_(None))
                    .order_by(Category.id.asc())
                    .with_for_update(read=True)
                )
            )
            .scalars()
            .all()
        )
        cats_by_lower: dict[str, Category] = {c.name.lower(): c for c in cat_rows}
        matched = cats_by_lower.get(cleaned_cat.lower())
        if matched is not None:
            resolved_category_id = matched.id
        elif create_category_if_missing:
            created_cat = await _get_or_create_category_by_name(
                session,
                cleaned_cat,
                now=now,
                cats_by_lower=cats_by_lower,
                events=events,
            )
            resolved_category_id = created_cat.id
        else:
            raise FeedCategoryNotFoundError(
                f"Category {cleaned_cat!r} not found (pass --create-category to create it)"
            )

    user_title = title.strip() if title and title.strip() else None

    # 3. Lock existing feed by feed_url FOR UPDATE
    existing = (
        await session.execute(sa.select(Feed).where(Feed.feed_url == cleaned_url).with_for_update())
    ).scalar_one_or_none()

    if existing is not None:
        feed = _reactivate_or_raise(
            existing,
            now=now,
            user_title=user_title,
            resolved_category_id=resolved_category_id,
            poll_interval_minutes=poll_interval_minutes,
            expiration_days_override=expiration_days_override,
            fetch_full_text=fetch_full_text,
            research_policy=research_policy,
            curation_instructions=curation_instructions,
            fields_set=fields_set,
        )
        reactivated = True
    else:
        default_poll = await _get_default_poll_interval(session)
        field_ts = {}
        if user_title is not None:
            final_title = user_title
            field_ts["title"] = now_iso
        else:
            final_title = default_title_from_url(cleaned_url)

        feed = Feed(
            id=uuid.uuid4(),
            category_id=resolved_category_id,
            feed_url=cleaned_url,
            site_url=None,
            title=final_title,
            status="active",
            health_state="healthy",
            poll_interval_minutes=poll_interval_minutes or default_poll,
            next_fetch_at=now,
            initial_backfill_done=False,
            expiration_days_override=expiration_days_override,
            fetch_full_text=fetch_full_text,
            research_policy=(
                dict(research_policy)
                if research_policy is not None
                else dict(DEFAULT_RESEARCH_POLICY)
            ),
            curation_instructions=curation_instructions,
            field_updated_at=field_ts,
            config_version=1,
            created_at=now,
            updated_at=now,
        )
        try:
            # Savepoint guards concurrent same-URL subscribe race on feeds.feed_url UNIQUE (G-06)
            async with session.begin_nested():
                session.add(feed)
                await session.flush()
            reactivated = False
        except IntegrityError:
            winner = (
                await session.execute(
                    sa.select(Feed).where(Feed.feed_url == cleaned_url).with_for_update()
                )
            ).scalar_one_or_none()
            if winner is None:
                raise
            feed = _reactivate_or_raise(
                winner,
                now=now,
                user_title=user_title,
                resolved_category_id=resolved_category_id,
                poll_interval_minutes=poll_interval_minutes,
                expiration_days_override=expiration_days_override,
                fetch_full_text=fetch_full_text,
                research_policy=research_policy,
                curation_instructions=curation_instructions,
                fields_set=fields_set,
            )
            reactivated = True

    await enqueue_job(
        session,
        job_type="poll_feed",
        dedupe_key=f"feed:{feed.id}",
        feed_id=feed.id,
        priority=10,
        bump_existing=True,
    )
    events.append(SyncEvent("feed", feed.id, "upsert"))
    await sync_log.record(session, events)
    return feed, reactivated


def _apply_opml_reactivation(
    existing: Feed,
    *,
    now: datetime,
    title: str | None,
    category_id: uuid.UUID | None,
    safe_site_url: str | None,
    events: list[SyncEvent],
    poll_feed_ids: list[uuid.UUID],
) -> None:
    """Reactivate an unsubscribed feed during OPML import (shared by snapshot & race paths)."""
    updated_fields = dict(existing.field_updated_at or {})
    apply_feed_status_transition(existing, "active", now)
    stamp_online_lww(updated_fields, "status", now, existing.created_at)
    if title:
        existing.title = title
        stamp_online_lww(updated_fields, "title", now, existing.created_at)
    if category_id is not None:
        existing.category_id = category_id
        stamp_online_lww(updated_fields, "category_id", now, existing.created_at)
    if safe_site_url:
        existing.site_url = safe_site_url
    existing.field_updated_at = updated_fields
    existing.updated_at = now
    events.append(SyncEvent("feed", existing.id, "upsert"))
    poll_feed_ids.append(existing.id)


async def import_opml_subscriptions(
    session: AsyncSession,
    ssrf_policy: SSRFPolicy,
    *,
    opml_xml: str,
    default_category_id: uuid.UUID | None = None,
) -> OpmlImportSummary:
    """Import OPML 2.0 subscriptions transactionally. Caller owns session.commit() (C6)."""
    if len(opml_xml.encode("utf-8")) > MAX_OPML_BYTES:
        raise FeedValidationError(f"OPML payload exceeds {MAX_OPML_BYTES} bytes")

    try:
        subs = parse_opml(opml_xml)
    except OpmlParseError as exc:
        raise FeedValidationError(str(exc)) from exc

    now: datetime = (await session.execute(sa.select(sa.func.now()))).scalar_one()
    now_iso = now.isoformat()
    default_poll = await _get_default_poll_interval(session)

    # Load existing active categories by lower(name) with FOR SHARE (categories -> feeds; C8, K-01)
    cat_rows = (
        (
            await session.execute(
                sa.select(Category)
                .where(Category.deleted_at.is_(None))
                .order_by(Category.id.asc())
                .with_for_update(read=True)
            )
        )
        .scalars()
        .all()
    )
    cats_by_lower: dict[str, Category] = {c.name.lower(): c for c in cat_rows}
    active_cat_ids: set[uuid.UUID] = {c.id for c in cat_rows}
    if default_category_id is not None and default_category_id not in active_cat_ids:
        raise FeedCategoryNotFoundError("Category not found")

    existing_feeds = (await session.execute(sa.select(Feed))).scalars().all()
    feeds_by_url: dict[str, Feed] = {f.feed_url: f for f in existing_feeds}

    imported = 0
    reactivated = 0
    skipped_duplicates = 0
    rejected_invalid = 0
    seen_in_opml: set[str] = set()
    events: list[SyncEvent] = []
    poll_feed_ids: list[uuid.UUID] = []

    for sub in subs:
        url = sub.feed_url.strip()
        if url in seen_in_opml:
            skipped_duplicates += 1
            continue
        seen_in_opml.add(url)

        try:
            # I-10 / E-10-F1 / E3: Entry-time syntax + literal IP check only during bulk import
            validate_feed_url_syntax(url)
            ssrf_policy.validate_url_and_literal_ip(url)
        except (SafeHttpError, ValueError):
            rejected_invalid += 1
            continue

        safe_site_url = sanitize_site_url(sub.site_url)

        category_id = default_category_id
        if sub.category_name and sub.category_name.strip():
            cat = await _get_or_create_category_by_name(
                session,
                sub.category_name,
                now=now,
                cats_by_lower=cats_by_lower,
                events=events,
            )
            category_id = cat.id

        existing = feeds_by_url.get(url)
        if existing is not None:
            if existing.status != "unsubscribed":
                skipped_duplicates += 1
                continue

            _apply_opml_reactivation(
                existing,
                now=now,
                title=sub.title,
                category_id=category_id,
                safe_site_url=safe_site_url,
                events=events,
                poll_feed_ids=poll_feed_ids,
            )
            reactivated += 1
            continue

        field_ts: dict[str, str] = {}
        if sub.title:
            title = sub.title
            field_ts["title"] = now_iso
        else:
            title = default_title_from_url(url)

        feed = Feed(
            id=uuid.uuid4(),
            category_id=category_id,
            feed_url=url,
            site_url=safe_site_url,
            title=title,
            status="active",
            health_state="healthy",
            poll_interval_minutes=default_poll,
            next_fetch_at=now,
            initial_backfill_done=False,
            fetch_full_text=True,
            research_policy=dict(DEFAULT_RESEARCH_POLICY),
            curation_instructions="",
            field_updated_at=field_ts,
            config_version=1,
            created_at=now,
            updated_at=now,
        )
        try:
            # Savepoint guards concurrent same-URL insert race on uq_feeds_feed_url (G-06-F1)
            async with session.begin_nested():
                session.add(feed)
                await session.flush()
        except IntegrityError:
            winner = (
                await session.execute(sa.select(Feed).where(Feed.feed_url == url).with_for_update())
            ).scalar_one_or_none()
            if winner is None:
                raise
            feeds_by_url[url] = winner
            if winner.status != "unsubscribed":
                skipped_duplicates += 1
                continue
            _apply_opml_reactivation(
                winner,
                now=now,
                title=sub.title,
                category_id=category_id,
                safe_site_url=safe_site_url,
                events=events,
                poll_feed_ids=poll_feed_ids,
            )
            reactivated += 1
            continue
        feeds_by_url[url] = feed
        events.append(SyncEvent("feed", feed.id, "upsert"))
        poll_feed_ids.append(feed.id)
        imported += 1

    for fid in poll_feed_ids:
        await enqueue_job(
            session,
            job_type="poll_feed",
            dedupe_key=f"feed:{fid}",
            feed_id=fid,
            priority=50,
            bump_existing=True,
        )

    if events:
        await sync_log.record(session, events)

    return OpmlImportSummary(
        imported=imported,
        reactivated=reactivated,
        skipped_duplicates=skipped_duplicates,
        rejected_invalid=rejected_invalid,
    )


async def export_opml_xml(session: AsyncSession) -> str:
    """Export active, paused, and muted subscriptions as a standard OPML 2.0 portability XML."""
    rows = (
        await session.execute(
            sa.select(Feed, Category.name)
            .outerjoin(
                Category,
                sa.and_(Category.id == Feed.category_id, Category.deleted_at.is_(None)),
            )
            .where(Feed.status.in_(("active", "paused", "muted")))
            .order_by(Category.sort_order.asc().nullsfirst(), Feed.title.asc())
        )
    ).all()

    grouped: dict[str | None, list[OpmlSubscription]] = {}
    for feed, cat_name in rows:
        grouped.setdefault(cat_name, []).append(
            OpmlSubscription(
                feed_url=feed.feed_url,
                title=feed.title,
                site_url=feed.site_url,
                category_name=cat_name,
            )
        )

    return export_opml(grouped)
