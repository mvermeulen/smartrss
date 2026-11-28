"""Feed CRUD, health, manual poll, and OPML import/export endpoints (§10.1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from smartrss.api.deps import get_current_device, get_db_session, get_ssrf_policy
from smartrss.api.schemas import (
    FeedCreateRequest,
    FeedPollTriggerResponse,
    FeedResponse,
    FeedUpdateRequest,
    OpmlImportRequest,
    OpmlImportResponse,
)
from smartrss.db.models import DEFAULT_RESEARCH_POLICY, AppSettings, Category, Device, Feed
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

router = APIRouter(prefix="/api/v1/feeds", tags=["feeds"])


def _stamp_online_lww(
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


def _stamp_research_policy_fields(
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
            _stamp_online_lww(field_ts, f"research_policy.{subkey}", db_now, fallback)
    if (
        prev.get("guidance") != new_rp.get("guidance")
        or prev.get("max_hops") != new_rp.get("max_hops")
        or "research_policy" not in field_ts
    ):
        _stamp_online_lww(field_ts, "research_policy", db_now, fallback)


def _sanitize_site_url(raw: str | None) -> str | None:
    """Keep site_url only if it is a valid http(s) URL; otherwise return None (I-11)."""
    if not raw or not raw.strip():
        return None
    cleaned = raw.strip()
    try:
        canonicalize_url(cleaned)
    except ValueError:
        return None
    return cleaned


async def _validate_feed_url(policy: SSRFPolicy, url: str) -> str:
    cleaned = url.strip()
    validate_feed_url_syntax(cleaned)
    _scheme, host, port = policy.validate_url(cleaned)
    await policy.resolve_and_validate(host, port)
    return cleaned


async def _get_default_poll_interval(session: AsyncSession) -> int:
    row = await session.get(AppSettings, 1)
    return row.default_poll_interval_minutes if row is not None else 60


@router.get("", response_model=list[FeedResponse])
async def list_feeds(
    status_filter: str | None = Query(default=None, alias="status"),
    health_state: str | None = Query(default=None),
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> list[FeedResponse]:
    stmt = sa.select(Feed)
    if status_filter is not None:
        stmt = stmt.where(Feed.status == status_filter)
    if health_state is not None:
        stmt = stmt.where(Feed.health_state == health_state)
    stmt = stmt.order_by(Feed.title.asc(), Feed.created_at.asc())
    rows = (await session.execute(stmt)).scalars().all()
    return [FeedResponse.model_validate(r) for r in rows]


@router.get("/health", response_model=list[FeedResponse])
async def feeds_health(
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> list[FeedResponse]:
    severity = sa.case(
        (Feed.health_state == "gone", 0),
        (Feed.health_state == "broken", 1),
        (Feed.health_state == "degraded", 2),
        else_=3,
    )
    rows = (
        (
            await session.execute(
                sa.select(Feed).order_by(
                    severity.asc(),
                    Feed.consecutive_errors.desc(),
                    Feed.last_success_at.asc().nullsfirst(),
                    Feed.title.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return [FeedResponse.model_validate(r) for r in rows]


@router.post("/import-opml", response_model=OpmlImportResponse)
async def import_opml_endpoint(
    body: OpmlImportRequest,
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
    ssrf_policy: SSRFPolicy = Depends(get_ssrf_policy),
) -> OpmlImportResponse:
    try:
        subs = parse_opml(body.opml_xml)
    except OpmlParseError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    now: datetime = (await session.execute(sa.select(sa.func.now()))).scalar_one()
    now_iso = now.isoformat()
    default_poll = await _get_default_poll_interval(session)

    # Load existing active categories by lower(name) with FOR SHARE (§5.1 categories -> feeds; K-01)
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
    if body.default_category_id is not None and body.default_category_id not in active_cat_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Category not found",
        )

    # Load existing feeds by feed_url
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
            # I-10 / E-10-F1: Entry-time syntax + literal IP check only during bulk OPML import
            # (avoid serial DNS inside the DB tx; full DNS + IP pinning runs at fetch)
            validate_feed_url_syntax(url)
            ssrf_policy.validate_url_and_literal_ip(url)
        except (SafeHttpError, ValueError):
            rejected_invalid += 1
            continue

        safe_site_url = _sanitize_site_url(sub.site_url)

        # Resolve category
        category_id = body.default_category_id
        if sub.category_name:
            cat_key = sub.category_name.strip().lower()
            cat = cats_by_lower.get(cat_key)
            if cat is None:
                cat = Category(
                    id=uuid.uuid4(),
                    name=sub.category_name.strip(),
                    sort_order=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(cat)
                await session.flush()
                cats_by_lower[cat_key] = cat
                events.append(SyncEvent("category", cat.id, "upsert"))
            category_id = cat.id

        existing = feeds_by_url.get(url)
        if existing is not None:
            if existing.status != "unsubscribed":
                skipped_duplicates += 1
                continue

            # Reactivate unsubscribed feed using shared transition & monotonic LWW stamps
            updated_fields = dict(existing.field_updated_at or {})
            apply_feed_status_transition(existing, "active", now)
            _stamp_online_lww(updated_fields, "status", now, existing.created_at)
            if sub.title:
                existing.title = sub.title
                _stamp_online_lww(updated_fields, "title", now, existing.created_at)
            if category_id is not None:
                existing.category_id = category_id
                _stamp_online_lww(updated_fields, "category_id", now, existing.created_at)
            if safe_site_url:
                existing.site_url = safe_site_url
            existing.field_updated_at = updated_fields
            existing.updated_at = now
            events.append(SyncEvent("feed", existing.id, "upsert"))
            poll_feed_ids.append(existing.id)
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
        session.add(feed)
        await session.flush()
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
    await session.commit()

    return OpmlImportResponse(
        imported=imported,
        reactivated=reactivated,
        skipped_duplicates=skipped_duplicates,
        rejected_invalid=rejected_invalid,
    )


@router.get("/export-opml")
async def export_opml_endpoint(
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
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

    xml_str = export_opml(grouped)
    return Response(content=xml_str, media_type="application/xml")


@router.post("", response_model=FeedResponse, status_code=status.HTTP_201_CREATED)
async def create_feed(
    body: FeedCreateRequest,
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
    ssrf_policy: SSRFPolicy = Depends(get_ssrf_policy),
) -> FeedResponse:
    try:
        feed_url = await _validate_feed_url(ssrf_policy, body.feed_url)
    except (SafeHttpError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid or blocked feed_url: {exc}",
        ) from exc

    if body.category_id is not None:
        cat = (
            await session.execute(
                sa.select(Category.id)
                .where(Category.id == body.category_id, Category.deleted_at.is_(None))
                .with_for_update(read=True)
            )
        ).scalar_one_or_none()
        if cat is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Category not found",
            )

    now: datetime = (await session.execute(sa.select(sa.func.now()))).scalar_one()
    now_iso = now.isoformat()
    user_title = body.title.strip() if body.title and body.title.strip() else None

    existing = (
        await session.execute(sa.select(Feed).where(Feed.feed_url == feed_url).with_for_update())
    ).scalar_one_or_none()

    if existing is not None:
        if existing.status != "unsubscribed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Feed URL is already subscribed",
            )
        field_ts = dict(existing.field_updated_at or {})
        apply_feed_status_transition(existing, "active", now)
        _stamp_online_lww(field_ts, "status", now, existing.created_at)
        if user_title is not None:
            existing.title = user_title
            _stamp_online_lww(field_ts, "title", now, existing.created_at)
        if body.category_id is not None:
            existing.category_id = body.category_id
            _stamp_online_lww(field_ts, "category_id", now, existing.created_at)
        if body.poll_interval_minutes is not None:
            existing.poll_interval_minutes = body.poll_interval_minutes
            _stamp_online_lww(field_ts, "poll_interval_minutes", now, existing.created_at)
        if body.expiration_days_override is not None:
            existing.expiration_days_override = body.expiration_days_override
            _stamp_online_lww(field_ts, "expiration_days_override", now, existing.created_at)
        if "fetch_full_text" in body.model_fields_set:
            existing.fetch_full_text = body.fetch_full_text
            _stamp_online_lww(field_ts, "fetch_full_text", now, existing.created_at)
        if body.research_policy is not None:
            new_rp = body.research_policy.model_dump()
            _stamp_research_policy_fields(
                field_ts, existing.research_policy, new_rp, now, existing.created_at
            )
            existing.research_policy = new_rp
        if (
            body.curation_instructions
            and body.curation_instructions != existing.curation_instructions
        ):
            existing.curation_instructions = body.curation_instructions
            existing.config_version += 1
        existing.field_updated_at = field_ts
        existing.updated_at = now
        feed = existing
    else:
        default_poll = await _get_default_poll_interval(session)
        field_ts = {}
        if user_title is not None:
            title = user_title
            field_ts["title"] = now_iso
        else:
            title = default_title_from_url(feed_url)

        feed = Feed(
            id=uuid.uuid4(),
            category_id=body.category_id,
            feed_url=feed_url,
            site_url=None,
            title=title,
            status="active",
            health_state="healthy",
            poll_interval_minutes=body.poll_interval_minutes or default_poll,
            next_fetch_at=now,
            initial_backfill_done=False,
            expiration_days_override=body.expiration_days_override,
            fetch_full_text=body.fetch_full_text,
            research_policy=(
                body.research_policy.model_dump()
                if body.research_policy is not None
                else dict(DEFAULT_RESEARCH_POLICY)
            ),
            curation_instructions=body.curation_instructions,
            field_updated_at=field_ts,
            config_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(feed)
        await session.flush()

    await enqueue_job(
        session,
        job_type="poll_feed",
        dedupe_key=f"feed:{feed.id}",
        feed_id=feed.id,
        priority=10,
        bump_existing=True,
    )
    await sync_log.record(session, [SyncEvent("feed", feed.id, "upsert")])
    await session.commit()
    return FeedResponse.model_validate(feed)


@router.get("/{feed_id}", response_model=FeedResponse)
async def get_feed(
    feed_id: uuid.UUID,
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> FeedResponse:
    feed = await session.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feed not found")
    return FeedResponse.model_validate(feed)


@router.patch("/{feed_id}", response_model=FeedResponse)
async def update_feed(
    feed_id: uuid.UUID,
    body: FeedUpdateRequest,
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
    ssrf_policy: SSRFPolicy = Depends(get_ssrf_policy),
) -> FeedResponse:
    fields_set = body.model_fields_set
    # Lock target category FOR SHARE *before* locking Feed FOR UPDATE (§5.1 order; K-01)
    category_exists = True
    if "category_id" in fields_set and body.category_id is not None:
        cat = (
            await session.execute(
                sa.select(Category.id)
                .where(Category.id == body.category_id, Category.deleted_at.is_(None))
                .with_for_update(read=True)
            )
        ).scalar_one_or_none()
        category_exists = cat is not None

    feed = (
        await session.execute(sa.select(Feed).where(Feed.id == feed_id).with_for_update())
    ).scalar_one_or_none()
    if feed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feed not found")

    now: datetime = (await session.execute(sa.select(sa.func.now()))).scalar_one()
    field_ts: dict[str, str] = dict(feed.field_updated_at or {})
    trigger_poll = False

    # Optimistic version check ONLY when curation_instructions changes
    if (
        "curation_instructions" in fields_set
        and body.curation_instructions is not None
        and body.curation_instructions != feed.curation_instructions
    ):
        if (
            body.expected_config_version is None
            or body.expected_config_version != feed.config_version
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "curation_instructions version conflict",
                    "current_config_version": feed.config_version,
                },
            )
        feed.curation_instructions = body.curation_instructions
        feed.config_version += 1

    # feed_url edit: validate via SSRFPolicy, check collision, reset health & backfill
    if "feed_url" in fields_set and body.feed_url is not None:
        try:
            new_url = await _validate_feed_url(ssrf_policy, body.feed_url)
        except (SafeHttpError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Invalid or blocked feed_url: {exc}",
            ) from exc

        if new_url != feed.feed_url:
            collision = (
                await session.execute(
                    sa.select(Feed.id).where(Feed.feed_url == new_url, Feed.id != feed.id)
                )
            ).scalar_one_or_none()
            if collision is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Feed URL already belongs to feed {collision}",
                )
            feed.feed_url = new_url
            feed.etag = None
            feed.last_modified = None
            feed.health_state = "healthy"
            feed.consecutive_errors = 0
            feed.last_error = None
            feed.next_fetch_at = now
            feed.initial_backfill_done = False
            _stamp_online_lww(field_ts, "feed_url", now, feed.created_at)
            trigger_poll = True

    if "category_id" in fields_set:
        if body.category_id is not None and not category_exists:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Category not found",
            )
        feed.category_id = body.category_id
        _stamp_online_lww(field_ts, "category_id", now, feed.created_at)

    if "title" in fields_set and body.title is not None:
        feed.title = body.title.strip()
        _stamp_online_lww(field_ts, "title", now, feed.created_at)

    if "status" in fields_set and body.status is not None:
        if apply_feed_status_transition(feed, body.status, now):
            trigger_poll = True
        _stamp_online_lww(field_ts, "status", now, feed.created_at)

    if "poll_interval_minutes" in fields_set and body.poll_interval_minutes is not None:
        feed.poll_interval_minutes = body.poll_interval_minutes
        _stamp_online_lww(field_ts, "poll_interval_minutes", now, feed.created_at)

    if "expiration_days_override" in fields_set:
        feed.expiration_days_override = body.expiration_days_override
        _stamp_online_lww(field_ts, "expiration_days_override", now, feed.created_at)

    if "fetch_full_text" in fields_set and body.fetch_full_text is not None:
        feed.fetch_full_text = body.fetch_full_text
        _stamp_online_lww(field_ts, "fetch_full_text", now, feed.created_at)

    if "research_policy" in fields_set and body.research_policy is not None:
        rp_dict: dict[str, Any] = body.research_policy.model_dump()
        _stamp_research_policy_fields(field_ts, feed.research_policy, rp_dict, now, feed.created_at)
        feed.research_policy = rp_dict

    feed.field_updated_at = field_ts
    feed.updated_at = now

    if trigger_poll:
        await enqueue_job(
            session,
            job_type="poll_feed",
            dedupe_key=f"feed:{feed.id}",
            feed_id=feed.id,
            priority=10,
            bump_existing=True,
        )

    await sync_log.record(session, [SyncEvent("feed", feed.id, "upsert")])
    await session.commit()
    return FeedResponse.model_validate(feed)


@router.post("/{feed_id}/poll", response_model=FeedPollTriggerResponse)
async def trigger_manual_poll(
    feed_id: uuid.UUID,
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> FeedPollTriggerResponse:
    feed = await session.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feed not found")

    job_id = await enqueue_job(
        session,
        job_type="poll_feed",
        dedupe_key=f"feed:{feed.id}",
        feed_id=feed.id,
        priority=10,
        bump_existing=True,
    )
    await session.commit()
    return FeedPollTriggerResponse(feed_id=feed.id, job_id=job_id, queued=job_id is not None)
