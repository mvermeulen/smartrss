"""Sync API endpoints: GET /bootstrap, GET /pull, POST /push (§6)."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from smartrss.api.deps import get_current_device, get_db_session
from smartrss.db.models import Device
from smartrss.sync.schemas import (
    MAX_PUSH_BODY_BYTES,
    SyncBootstrapResponse,
    SyncPullResponse,
    SyncPushRequest,
    SyncPushResponse,
)
from smartrss.sync.service import PushTransientError, sync_bootstrap, sync_pull, sync_push

router = APIRouter(prefix="/api/v1/sync", tags=["sync"])


@router.get("/bootstrap", response_model=SyncBootstrapResponse)
async def bootstrap_endpoint(
    cursor: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=1000),
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> SyncBootstrapResponse:
    return await sync_bootstrap(session, cursor=cursor, limit=limit)


@router.get("/pull", response_model=SyncPullResponse)
async def pull_endpoint(
    since_seq: int = Query(..., ge=0),
    epoch: uuid.UUID = Query(...),
    limit: int = Query(default=200, ge=1, le=500),
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> SyncPullResponse:
    return await sync_pull(session, since_seq=since_seq, epoch=epoch, limit=limit)


@router.post("/push", response_model=SyncPushResponse)
async def push_endpoint(
    request: Request,
    device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> SyncPushResponse:
    # Enforce 2 MiB maximum request-body size before JSON/Pydantic parsing (S6, P-09)
    content_length_raw = request.headers.get("content-length")
    if content_length_raw is not None:
        try:
            if int(content_length_raw.strip()) > MAX_PUSH_BODY_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=f"Push payload exceeds maximum size of {MAX_PUSH_BODY_BYTES} bytes",
                )
        except ValueError:
            pass

    chunks: list[bytes] = []
    total_bytes = 0
    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > MAX_PUSH_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"Push payload exceeds maximum size of {MAX_PUSH_BODY_BYTES} bytes",
            )
        chunks.append(chunk)

    raw_body = b"".join(chunks)
    try:
        parsed_json = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid JSON payload: {exc}",
        ) from exc

    try:
        push_request = SyncPushRequest.model_validate(parsed_json)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=json.loads(exc.json(include_input=False)),
        ) from exc

    try:
        return await sync_push(session, device_id=device.id, request=push_request)
    except PushTransientError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
            headers={"Retry-After": "1"},
        ) from exc
