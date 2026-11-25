"""Minimal Milestone 1a item listing & detail inspection endpoints."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from smartrss.api.deps import get_current_device, get_db_session
from smartrss.api.schemas import ItemListResponse, ItemResponse
from smartrss.db.models import Device, Item

router = APIRouter(prefix="/api/v1/items", tags=["items"])


@router.get("", response_model=ItemListResponse)
async def list_items(
    feed_id: uuid.UUID | None = Query(default=None),
    state: str | None = Query(default=None),
    is_starred: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> ItemListResponse:
    filters = []
    if feed_id is not None:
        filters.append(Item.feed_id == feed_id)
    if state is not None:
        filters.append(Item.state == state)
    if is_starred is not None:
        filters.append(Item.is_starred == is_starred)

    total = (
        await session.execute(sa.select(sa.func.count()).select_from(Item).where(*filters))
    ).scalar_one()

    rows = (
        (
            await session.execute(
                sa.select(Item)
                .where(*filters)
                .order_by(Item.fetched_at.desc(), Item.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    return ItemListResponse(
        items=[ItemResponse.model_validate(r) for r in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get("/{item_id}", response_model=ItemResponse)
async def get_item(
    item_id: uuid.UUID,
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> ItemResponse:
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    return ItemResponse.model_validate(item)
