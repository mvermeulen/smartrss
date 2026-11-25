"""Device pairing and per-device bearer token management endpoints."""

from __future__ import annotations

import uuid
from typing import Literal, cast

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from smartrss.api.deps import (
    get_current_device,
    get_db_session,
    get_pair_rate_limiter,
)
from smartrss.api.schemas import (
    CreatePairingCodeRequest,
    CreatePairingCodeResponse,
    DeviceResponse,
    PairExchangeRequest,
    PairExchangeResponse,
)
from smartrss.db.models import Device
from smartrss.security.auth import (
    GlobalSlidingWindowRateLimiter,
    InvalidPairingCodeError,
    create_pairing_code,
    redeem_pairing_code,
    revoke_device,
)

router = APIRouter(prefix="/api/v1", tags=["auth"])


@router.post("/auth/pair", response_model=PairExchangeResponse)
async def exchange_pairing_code(
    body: PairExchangeRequest,
    session: AsyncSession = Depends(get_db_session),
    rate_limiter: GlobalSlidingWindowRateLimiter = Depends(get_pair_rate_limiter),
) -> PairExchangeResponse:
    allowed, retry_after = await rate_limiter.check_and_record()
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many pairing attempts; try again later",
            headers={"Retry-After": str(retry_after)},
        )

    try:
        paired = await redeem_pairing_code(
            session,
            raw_code=body.code,
            device_name=body.device_name,
            platform=body.platform,
        )
    except InvalidPairingCodeError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    await session.commit()
    return PairExchangeResponse(
        device_id=paired.device.id,
        device_name=paired.device.name,
        platform=cast(Literal["ubuntu", "android", "web"], paired.device.platform),
        bearer_token=paired.bearer_token,
    )


@router.post(
    "/auth/pairing-codes",
    response_model=CreatePairingCodeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def issue_pairing_code(
    body: CreatePairingCodeRequest,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> CreatePairingCodeResponse:
    created = await create_pairing_code(
        session,
        device_name=body.device_name,
        platform=body.platform,
        created_by_device_id=device.id,
    )
    await session.commit()
    return CreatePairingCodeResponse(code=created.code, expires_at=created.expires_at)


@router.get("/devices", response_model=list[DeviceResponse])
async def list_devices(
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> list[DeviceResponse]:
    rows = (
        (await session.execute(sa.select(Device).order_by(Device.created_at.desc())))
        .scalars()
        .all()
    )
    return [DeviceResponse.model_validate(row) for row in rows]


@router.post("/devices/{device_id}/revoke", response_model=DeviceResponse)
async def revoke_device_endpoint(
    device_id: uuid.UUID,
    _response: Response,
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> DeviceResponse:
    revoked = await revoke_device(session, device_id)
    if revoked is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    await session.commit()
    return DeviceResponse.model_validate(revoked)
