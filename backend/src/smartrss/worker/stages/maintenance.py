"""Operational retention cleanup stage (retention_cleanup, §9.3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from smartrss.db.models import AppSettings, Job, PipelineRun, StageMetric, SyncChange
from smartrss.db.sync_log import execute_synced

JOBS_RETENTION_DAYS = 30
SYNC_CHANGES_RETENTION_DAYS = 90
METRICS_RETENTION_DAYS = 180


@dataclass(frozen=True, slots=True)
class RetentionCleanupResult:
    jobs_deleted: int
    sync_changes_deleted: int
    stage_metrics_deleted: int
    pipeline_runs_deleted: int


async def run_retention_cleanup(session: AsyncSession) -> RetentionCleanupResult:
    """Delete operational history past retention windows in FK-safe order.

    Updates app_settings.last_retention_cleanup_at without emitting a sync event.
    """
    now = datetime.now(UTC)

    jobs_res = await session.execute(
        sa.delete(Job).where(
            Job.status == "done",
            Job.finished_at < now - timedelta(days=JOBS_RETENTION_DAYS),
        )
    )
    sync_res = await session.execute(
        sa.delete(SyncChange).where(
            SyncChange.changed_at < now - timedelta(days=SYNC_CHANGES_RETENTION_DAYS)
        )
    )
    metrics_res = await session.execute(
        sa.delete(StageMetric).where(
            StageMetric.created_at < now - timedelta(days=METRICS_RETENTION_DAYS)
        )
    )
    runs_res = await session.execute(
        sa.delete(PipelineRun).where(
            PipelineRun.started_at < now - timedelta(days=METRICS_RETENTION_DAYS)
        )
    )

    # Internal operational timestamp: intentionally emits zero sync_changes events
    await execute_synced(
        session,
        sa.update(AppSettings).where(AppSettings.id == 1).values(last_retention_cleanup_at=now),
        lambda _res: [],
    )

    return RetentionCleanupResult(
        jobs_deleted=int(cast(CursorResult[Any], jobs_res).rowcount or 0),
        sync_changes_deleted=int(cast(CursorResult[Any], sync_res).rowcount or 0),
        stage_metrics_deleted=int(cast(CursorResult[Any], metrics_res).rowcount or 0),
        pipeline_runs_deleted=int(cast(CursorResult[Any], runs_res).rowcount or 0),
    )
