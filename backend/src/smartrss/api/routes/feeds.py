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
from smartrss.db.models import Category, Device, Feed
from smartrss.db.sync_log import SyncEvent, sync_log
from smartrss.domain.sync_merge import apply_feed_status_transition
from smartrss.feeds.service import (
    FeedAlreadySubscribedError,
    FeedCategoryNotFoundError,
    FeedValidationError,
    create_or_reactivate_feed,
    export_opml_xml,
    import_opml_subscriptions,
    list_feeds_query,
    stamp_online_lww,
    stamp_research_policy_fields,
    validate_feed_url,
)
from smartrss.jobs.queue import enqueue_job
from smartrss.security.safe_http import SSRFPolicy

router = APIRouter(prefix="/api/v1/feeds", tags=["feeds"])


@router.get("", response_model=list[FeedResponse])
async def list_feeds(
    status_filter: str | None = Query(default=None, alias="status"),
    health_state: str | None = Query(default=None),
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> list[FeedResponse]:
    rows = await list_feeds_query(session, status_filter=status_filter, health_state=health_state)
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
        summary = await import_opml_subscriptions(
            session,
            ssrf_policy,
            opml_xml=body.opml_xml,
            default_category_id=body.default_category_id,
        )
    except (FeedValidationError, FeedCategoryNotFoundError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    await session.commit()
    return OpmlImportResponse(
        imported=summary.imported,
        reactivated=summary.reactivated,
        skipped_duplicates=summary.skipped_duplicates,
        rejected_invalid=summary.rejected_invalid,
    )


@router.get("/export-opml")
async def export_opml_endpoint(
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    xml_str = await export_opml_xml(session)
    return Response(content=xml_str, media_type="application/xml")


@router.post("", response_model=FeedResponse, status_code=status.HTTP_201_CREATED)
async def create_feed(
    body: FeedCreateRequest,
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
    ssrf_policy: SSRFPolicy = Depends(get_ssrf_policy),
) -> FeedResponse:
    try:
        feed, _reactivated = await create_or_reactivate_feed(
            session,
            ssrf_policy,
            feed_url=body.feed_url,
            title=body.title,
            category_id=body.category_id,
            poll_interval_minutes=body.poll_interval_minutes,
            expiration_days_override=body.expiration_days_override,
            fetch_full_text=body.fetch_full_text,
            research_policy=(
                body.research_policy.model_dump() if body.research_policy is not None else None
            ),
            curation_instructions=body.curation_instructions,
            fields_set=body.model_fields_set,
        )
    except (FeedValidationError, FeedCategoryNotFoundError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except FeedAlreadySubscribedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Feed URL is already subscribed",
        ) from exc

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
            new_url = await validate_feed_url(ssrf_policy, body.feed_url)
        except FeedValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
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
            stamp_online_lww(field_ts, "feed_url", now, feed.created_at)
            trigger_poll = True

    if "category_id" in fields_set:
        if body.category_id is not None and not category_exists:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Category not found",
            )
        feed.category_id = body.category_id
        stamp_online_lww(field_ts, "category_id", now, feed.created_at)

    if "title" in fields_set and body.title is not None:
        feed.title = body.title.strip()
        stamp_online_lww(field_ts, "title", now, feed.created_at)

    if "status" in fields_set and body.status is not None:
        if apply_feed_status_transition(feed, body.status, now):
            trigger_poll = True
        stamp_online_lww(field_ts, "status", now, feed.created_at)

    if "poll_interval_minutes" in fields_set and body.poll_interval_minutes is not None:
        feed.poll_interval_minutes = body.poll_interval_minutes
        stamp_online_lww(field_ts, "poll_interval_minutes", now, feed.created_at)

    if "expiration_days_override" in fields_set:
        feed.expiration_days_override = body.expiration_days_override
        stamp_online_lww(field_ts, "expiration_days_override", now, feed.created_at)

    if "fetch_full_text" in fields_set and body.fetch_full_text is not None:
        feed.fetch_full_text = body.fetch_full_text
        stamp_online_lww(field_ts, "fetch_full_text", now, feed.created_at)

    if "research_policy" in fields_set and body.research_policy is not None:
        rp_dict: dict[str, Any] = body.research_policy.model_dump()
        stamp_research_policy_fields(field_ts, feed.research_policy, rp_dict, now, feed.created_at)
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
