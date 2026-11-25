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
from smartrss.db.sync_log import SyncEvent, execute_synced, sync_log

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

    now = datetime.now(UTC)
    now_iso = now.isoformat()
    cat.deleted_at = now
    cat.updated_at = now

    # Clear category_id and stamp field_updated_at['category_id'] = now on affected child feeds
    stmt = (
        sa.update(Feed)
        .where(Feed.category_id == category_id)
        .values(
            category_id=None,
            field_updated_at=sa.func.jsonb_set(
                Feed.field_updated_at,
                sa.text("'{category_id}'::text[]"),
                sa.func.to_jsonb(sa.cast(now_iso, sa.Text())),
                True,
            ),
            updated_at=now,
        )
        .returning(Feed.id)
    )
    _, feed_events = await execute_synced(
        session,
        stmt,
        lambda res: [SyncEvent("feed", fid, "upsert") for fid in res.scalars().all()],
    )

    events = [SyncEvent("category", category_id, "delete"), *feed_events]
    await sync_log.record(session, events)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
