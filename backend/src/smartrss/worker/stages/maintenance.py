"""Operational retention cleanup stage (retention_cleanup, 1a §9.3 & 1b §5.4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from smartrss.db.models import (
    AppSettings,
    Job,
    PipelineRun,
    StageMetric,
    SyncChange,
    SyncMutation,
    SyncState,
)
from smartrss.db.sync_log import execute_synced

JOBS_RETENTION_DAYS = 30
SYNC_CHANGES_RETENTION_DAYS = 90
SYNC_MUTATIONS_RETENTION_DAYS = 90
METRICS_RETENTION_DAYS = 180


@dataclass(frozen=True, slots=True)
class RetentionCleanupResult:
    jobs_deleted: int
    sync_changes_deleted: int
    stage_metrics_deleted: int
    pipeline_runs_deleted: int
    sync_mutations_deleted: int = 0
    pruned_through_seq: int | None = None


async def run_retention_cleanup(session: AsyncSession) -> RetentionCleanupResult:
    """Delete operational history past retention windows in FK-safe & §5.1 lock-safe order.

    Acquires app_settings BEFORE sync_state (§5.1, P-04), prunes sync_changes strictly by
    contiguous prefix, advances sync_state.pruned_through_seq atomically, and prunes
    sync_mutations older than 90 days.
    """
    db_now: datetime = (await session.execute(sa.select(sa.func.now()))).scalar_one()

    # 1. Operational housekeeping on jobs / metrics / runs (FK-safe: stage_metrics before runs)
    jobs_res = await session.execute(
        sa.delete(Job).where(
            Job.status == "done",
            Job.finished_at < sa.func.now() - sa.text(f"INTERVAL '{JOBS_RETENTION_DAYS} days'"),
        )
    )
    metrics_res = await session.execute(
        sa.delete(StageMetric).where(
            StageMetric.created_at
            < sa.func.now() - sa.text(f"INTERVAL '{METRICS_RETENTION_DAYS} days'")
        )
    )
    runs_res = await session.execute(
        sa.delete(PipelineRun).where(
            PipelineRun.started_at
            < sa.func.now() - sa.text(f"INTERVAL '{METRICS_RETENTION_DAYS} days'")
        )
    )

    # 2. Update app_settings BEFORE acquiring sync_state (§5.1 global lock order, P-04)
    await execute_synced(
        session,
        sa.update(AppSettings).where(AppSettings.id == 1).values(last_retention_cleanup_at=db_now),
        lambda _res: [],
    )

    # 3. Prune sync_mutations older than 90 days
    muts_res = await session.execute(
        sa.delete(SyncMutation).where(
            SyncMutation.server_applied_at
            < sa.func.now() - sa.text(f"INTERVAL '{SYNC_MUTATIONS_RETENTION_DAYS} days'")
        )
    )

    # 4. Contiguous-prefix sync_changes prune + advance sync_state.pruned_through_seq LAST (§5.4)
    cutoff_seq: int | None = (
        await session.execute(
            sa.select(sa.func.max(SyncChange.seq)).where(
                SyncChange.changed_at
                < sa.func.now() - sa.text(f"INTERVAL '{SYNC_CHANGES_RETENTION_DAYS} days'")
            )
        )
    ).scalar_one()

    sync_changes_deleted = 0
    pruned_through: int | None = None
    if cutoff_seq is not None:
        sync_res = await session.execute(sa.delete(SyncChange).where(SyncChange.seq <= cutoff_seq))
        sync_changes_deleted = int(cast(CursorResult[Any], sync_res).rowcount or 0)
        pruned_through = (
            await session.execute(
                sa.update(SyncState)
                .where(SyncState.id == 1)
                .values(
                    pruned_through_seq=sa.func.greatest(SyncState.pruned_through_seq, cutoff_seq),
                    updated_at=sa.func.now(),
                )
                .returning(SyncState.pruned_through_seq)
            )
        ).scalar_one()

    return RetentionCleanupResult(
        jobs_deleted=int(cast(CursorResult[Any], jobs_res).rowcount or 0),
        sync_changes_deleted=sync_changes_deleted,
        stage_metrics_deleted=int(cast(CursorResult[Any], metrics_res).rowcount or 0),
        pipeline_runs_deleted=int(cast(CursorResult[Any], runs_res).rowcount or 0),
        sync_mutations_deleted=int(cast(CursorResult[Any], muts_res).rowcount or 0),
        pruned_through_seq=pruned_through,
    )
