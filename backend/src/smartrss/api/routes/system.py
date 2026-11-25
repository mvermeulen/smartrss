"""System health, pipeline metrics, and dead-letter job management endpoints (§10.1)."""

from __future__ import annotations

from typing import Any, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from smartrss.api.deps import get_current_device, get_db_session, get_engine
from smartrss.api.schemas import (
    DeadLetterJobResponse,
    DeadLetterListResponse,
    PipelineRunResponse,
    SystemHealthResponse,
)
from smartrss.db.engine import check_db_health
from smartrss.db.models import Device, Job, PipelineRun, SyncState
from smartrss.jobs.metrics import query_metrics_summary
from smartrss.jobs.queue import (
    ActiveJobCollisionError,
    dismiss_dead_letter_job,
    retry_dead_letter_job,
)

router = APIRouter(prefix="/api/v1/system", tags=["system"])


@router.get("/health", response_model=SystemHealthResponse)
async def system_health(
    _device: Device = Depends(get_current_device),
    engine: AsyncEngine = Depends(get_engine),
    session: AsyncSession = Depends(get_db_session),
) -> SystemHealthResponse:
    db_ok = await check_db_health(engine)
    sync_state = await session.get(SyncState, 1)
    assert sync_state is not None

    q_rows = (
        await session.execute(
            sa.select(Job.job_type, Job.status, sa.func.count()).group_by(Job.job_type, Job.status)
        )
    ).all()
    queue_counts: dict[str, dict[str, int]] = {}
    dead_letter_count = 0
    for job_type, job_status, cnt in q_rows:
        queue_counts.setdefault(job_type, {})[job_status] = int(cnt)
        if job_status == "failed":
            dead_letter_count += int(cnt)

    recent_runs = (
        (
            await session.execute(
                sa.select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(10)
            )
        )
        .scalars()
        .all()
    )

    return SystemHealthResponse(
        db="ok" if db_ok else "error",
        instance_epoch=sync_state.instance_epoch,
        current_seq=sync_state.current_seq,
        queue_counts=queue_counts,
        dead_letter_count=dead_letter_count,
        recent_runs=[PipelineRunResponse.model_validate(r) for r in recent_runs],
    )


@router.get("/metrics")
async def system_metrics(
    window: Literal["24h", "7d", "30d"] = Query(default="24h"),
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    return await query_metrics_summary(session, window=window)


@router.get("/jobs/dead-letter", response_model=DeadLetterListResponse)
async def list_dead_letter(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> DeadLetterListResponse:
    total = (
        await session.execute(
            sa.select(sa.func.count()).select_from(Job).where(Job.status == "failed")
        )
    ).scalar_one()

    rows = (
        (
            await session.execute(
                sa.select(Job)
                .where(Job.status == "failed")
                .order_by(Job.finished_at.desc().nullslast(), Job.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    return DeadLetterListResponse(
        jobs=[DeadLetterJobResponse.model_validate(r) for r in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.post("/jobs/{job_id}/retry", response_model=DeadLetterJobResponse)
async def retry_dead_letter(
    job_id: int,
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> DeadLetterJobResponse:
    try:
        job = await retry_dead_letter_job(session, job_id)
    except ActiveJobCollisionError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "active_job_id": exc.active_job_id,
            },
        ) from exc

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dead-letter job not found",
        )

    await session.commit()
    return DeadLetterJobResponse.model_validate(job)


@router.post("/jobs/{job_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_dead_letter(
    job_id: int,
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    deleted = await dismiss_dead_letter_job(session, job_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dead-letter job not found",
        )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
