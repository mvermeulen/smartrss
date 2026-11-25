"""Category CRUD endpoints with sync_changes emission and child LWW stamping."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smartrss.api.deps import get_current_device, get_db_session
from smartrss.api.schemas import (
    CategoryCreateRequest,
    CategoryResponse,
    CategoryUpdateRequest,
)
from smartrss.db.models import Category, Device, Feed
from smartrss.db.sync_log import SyncEvent, sync_log
from smartrss.domain.sync_merge import compute_online_lww_timestamp, parse_field_timestamp

router = APIRouter(prefix="/api/v1/categories", tags=["categories"])


@router.get("", response_model=list[CategoryResponse])
async def list_categories(
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> list[CategoryResponse]:
    rows = (
        (
            await session.execute(
                sa.select(Category)
                .where(Category.deleted_at.is_(None))
                .order_by(Category.sort_order.asc(), Category.name.asc())
            )
        )
        .scalars()
        .all()
    )
    return [CategoryResponse.model_validate(r) for r in rows]


@router.post("", response_model=CategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_category(
    body: CategoryCreateRequest,
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> CategoryResponse:
    now = datetime.now(UTC)
    cat = Category(
        id=uuid.uuid4(),
        name=body.name.strip(),
        sort_order=body.sort_order,
        created_at=now,
        updated_at=now,
    )
    session.add(cat)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A category with that name already exists",
        ) from exc

    await sync_log.record(session, [SyncEvent("category", cat.id, "upsert")])
    await session.commit()
    return CategoryResponse.model_validate(cat)


@router.patch("/{category_id}", response_model=CategoryResponse)
async def update_category(
    category_id: uuid.UUID,
    body: CategoryUpdateRequest,
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> CategoryResponse:
    cat = (
        await session.execute(
            sa.select(Category).where(
                Category.id == category_id,
                Category.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if cat is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")

    if body.name is not None:
        cat.name = body.name.strip()
    if body.sort_order is not None:
        cat.sort_order = body.sort_order
    cat.updated_at = datetime.now(UTC)

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A category with that name already exists",
        ) from exc

    await sync_log.record(session, [SyncEvent("category", cat.id, "upsert")])
    await session.commit()
    return CategoryResponse.model_validate(cat)


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: uuid.UUID,
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    cat = (
        await session.execute(
            sa.select(Category)
            .where(Category.id == category_id, Category.deleted_at.is_(None))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cat is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")

    db_now: datetime = (await session.execute(sa.select(sa.func.now()))).scalar_one()
    cat.deleted_at = db_now
    cat.updated_at = db_now

    # Lock affected child feeds in id ASC order (§5.1) and stamp field_updated_at['category_id']
    # monotonically using PostgreSQL now() (P-02, C5, C7).
    child_feeds = (
        (
            await session.execute(
                sa.select(Feed)
                .where(Feed.category_id == category_id)
                .order_by(Feed.id.asc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    feed_events: list[SyncEvent] = []
    for feed in child_feeds:
        field_ts = dict(feed.field_updated_at or {})
        stored_ts = (
            parse_field_timestamp(field_ts, "category_id", feed.created_at)
            if "category_id" in field_ts
            else None
        )
        field_ts["category_id"] = compute_online_lww_timestamp(db_now, stored_ts).isoformat()
        feed.category_id = None
        feed.field_updated_at = field_ts
        feed.updated_at = db_now
        feed_events.append(SyncEvent("feed", feed.id, "upsert"))

    await session.flush()
    events = [SyncEvent("category", category_id, "delete"), *feed_events]
    await sync_log.record(session, events)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
