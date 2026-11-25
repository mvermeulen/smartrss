"""Pipeline runs & stage_metrics recording and SQL rollup queries (§8, §10.1)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from smartrss.db.models import PipelineRun

WINDOW_INTERVALS: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


@dataclass(frozen=True, slots=True)
class StageLatencyRow:
    job_type: str
    total_count: int
    ok_count: int
    failed_count: int
    skipped_count: int
    failure_rate: float
    avg_duration_ms: float
    p50_duration_ms: float
    p95_duration_ms: float
    max_duration_ms: int
    avg_queue_wait_ms: float


@dataclass(frozen=True, slots=True)
class FeedCostRow:
    feed_id: uuid.UUID
    feed_title: str | None
    total_jobs: int
    failed_jobs: int
    total_duration_ms: int
    avg_duration_ms: float


async def create_pipeline_run(
    session: AsyncSession,
    *,
    trigger: str,
    stages: Sequence[str],
) -> PipelineRun:
    """Insert a new running pipeline_runs row."""
    run = PipelineRun(
        id=uuid.uuid4(),
        trigger=trigger,
        stages=list(stages),
        status="running",
        started_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    return run


async def finalize_pipeline_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    status: str,
    items_polled: int = 0,
    items_extracted: int = 0,
    jobs_failed: int = 0,
    notes: str | None = None,
) -> PipelineRun:
    """Finalize a pipeline_runs row with elapsed duration and counters."""
    run = await session.get(PipelineRun, run_id)
    assert run is not None
    finished = datetime.now(UTC)
    duration_ms = max(0, int((finished - run.started_at).total_seconds() * 1000))
    run.status = status
    run.finished_at = finished
    run.duration_ms = duration_ms
    run.items_polled = items_polled
    run.items_extracted = items_extracted
    run.jobs_failed = jobs_failed
    run.notes = notes
    await session.flush()
    return run


async def query_metrics_summary(
    session: AsyncSession,
    *,
    window: str = "24h",
) -> dict[str, Any]:
    """Compute exact SQL p50/p95/avg/max/failure_rate per stage and per-feed cost rollup."""
    if window not in WINDOW_INTERVALS:
        raise ValueError(f"Unsupported metrics window: {window!r}")

    since = datetime.now(UTC) - WINDOW_INTERVALS[window]

    stage_sql = sa.text(
        """
        SELECT
            job_type,
            COUNT(*)::int AS total_count,
            COUNT(*) FILTER (WHERE status = 'ok')::int AS ok_count,
            COUNT(*) FILTER (WHERE status = 'failed')::int AS failed_count,
            COUNT(*) FILTER (WHERE status = 'skipped')::int AS skipped_count,
            COALESCE(AVG(duration_ms), 0)::float8 AS avg_duration_ms,
            COALESCE(
                percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms), 0
            )::float8 AS p50_duration_ms,
            COALESCE(
                percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms), 0
            )::float8 AS p95_duration_ms,
            COALESCE(MAX(duration_ms), 0)::int AS max_duration_ms,
            COALESCE(AVG(queue_wait_ms), 0)::float8 AS avg_queue_wait_ms
        FROM stage_metrics
        WHERE created_at >= :since
        GROUP BY job_type
        ORDER BY job_type
        """
    )
    stage_rows = (await session.execute(stage_sql, {"since": since})).mappings().all()

    stages = []
    for row in stage_rows:
        total = int(row["total_count"])
        failed = int(row["failed_count"])
        stages.append(
            {
                "job_type": row["job_type"],
                "total_count": total,
                "ok_count": int(row["ok_count"]),
                "failed_count": failed,
                "skipped_count": int(row["skipped_count"]),
                "failure_rate": round(failed / total, 4) if total > 0 else 0.0,
                "avg_duration_ms": round(float(row["avg_duration_ms"]), 2),
                "p50_duration_ms": round(float(row["p50_duration_ms"]), 2),
                "p95_duration_ms": round(float(row["p95_duration_ms"]), 2),
                "max_duration_ms": int(row["max_duration_ms"]),
                "avg_queue_wait_ms": round(float(row["avg_queue_wait_ms"]), 2),
            }
        )

    feed_sql = sa.text(
        """
        SELECT
            sm.feed_id,
            f.title AS feed_title,
            COUNT(*)::int AS total_jobs,
            COUNT(*) FILTER (WHERE sm.status = 'failed')::int AS failed_jobs,
            COALESCE(SUM(sm.duration_ms), 0)::bigint AS total_duration_ms,
            COALESCE(AVG(sm.duration_ms), 0)::float8 AS avg_duration_ms
        FROM stage_metrics sm
        LEFT JOIN feeds f ON f.id = sm.feed_id
        WHERE sm.created_at >= :since
          AND sm.feed_id IS NOT NULL
        GROUP BY sm.feed_id, f.title
        ORDER BY total_duration_ms DESC
        LIMIT 50
        """
    )
    feed_rows = (await session.execute(feed_sql, {"since": since})).mappings().all()
    per_feed = [
        {
            "feed_id": str(row["feed_id"]),
            "feed_title": row["feed_title"],
            "total_jobs": int(row["total_jobs"]),
            "failed_jobs": int(row["failed_jobs"]),
            "total_duration_ms": int(row["total_duration_ms"]),
            "avg_duration_ms": round(float(row["avg_duration_ms"]), 2),
        }
        for row in feed_rows
    ]

    return {
        "window": window,
        "since": since.isoformat(),
        "stages": stages,
        "per_feed": per_feed,
    }
