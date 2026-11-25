"""Online settings endpoints: GET /api/v1/settings and PATCH /api/v1/settings (§7)."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from smartrss.api.deps import get_current_device, get_db_session
from smartrss.api.schemas import AppSettingsResponse, AppSettingsUpdateRequest
from smartrss.db.models import AppSettings, Device
from smartrss.db.sync_log import APP_SETTINGS_ENTITY_ID, SyncEvent, sync_log

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


@router.get("", response_model=AppSettingsResponse)
async def get_app_settings(
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> AppSettingsResponse:
    row = await session.get(AppSettings, 1)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="app_settings singleton row is missing",
        )
    return AppSettingsResponse.model_validate(row)


@router.patch("", response_model=AppSettingsResponse)
async def update_app_settings(
    body: AppSettingsUpdateRequest,
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> AppSettingsResponse:
    row = (
        await session.execute(sa.select(AppSettings).where(AppSettings.id == 1).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="app_settings singleton row is missing",
        )

    changed = False
    for field_name in (
        "default_expiration_days",
        "backfill_cap",
        "default_poll_interval_minutes",
    ):
        if field_name in body.model_fields_set:
            new_val = getattr(body, field_name)
            if new_val is not None and getattr(row, field_name) != new_val:
                setattr(row, field_name, new_val)
                changed = True

    if changed:
        db_now: datetime = (await session.execute(sa.select(sa.func.now()))).scalar_one()
        row.updated_at = db_now
        await session.flush()
        await sync_log.record(
            session, [SyncEvent("app_settings", APP_SETTINGS_ENTITY_ID, "upsert")]
        )
        await session.commit()

    return AppSettingsResponse.model_validate(row)
