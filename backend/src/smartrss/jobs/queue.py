"""PostgreSQL SKIP LOCKED job queue with dedupe keys, lease fencing, and reaper (§8.1)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smartrss.db.models import Job, StageMetric
from smartrss.domain.backoff import compute_job_retry_delay
from smartrss.jobs.locks import LOCK_REAPER, try_advisory_xact_lock

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 120
DEFAULT_MAX_ATTEMPTS = 5
LEASE_EXPIRED_ERROR = "lease expired (worker crash/timeout)"


class ActiveJobCollisionError(RuntimeError):
    """Raised when retrying a dead-letter job conflicts with an active queued/running job."""

    def __init__(self, active_job_id: int | None) -> None:
        super().__init__("An active job with the same (job_type, dedupe_key) already exists")
        self.active_job_id = active_job_id


@dataclass(frozen=True, slots=True)
class ReapResult:
    requeued_count: int
    dead_lettered_count: int
    dead_lettered_poll_feeds: tuple[uuid.UUID, ...]
    dead_lettered_extract_items: tuple[uuid.UUID, ...]


async def enqueue_job(
    session: AsyncSession,
    *,
    job_type: str,
    dedupe_key: str,
    payload: dict[str, Any] | None = None,
    feed_id: uuid.UUID | None = None,
    item_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    priority: int = 50,
    run_after: datetime | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    bump_existing: bool = False,
) -> int | None:
    """Enqueue a deduplicated job. Returns job id if inserted/bumped, else None."""
    effective_run_after: Any = run_after if run_after is not None else sa.func.now()
    stmt = postgresql.insert(Job).values(
        job_type=job_type,
        dedupe_key=dedupe_key,
        payload=payload or {},
        feed_id=feed_id,
        item_id=item_id,
        run_id=run_id,
        priority=priority,
        status="queued",
        max_attempts=max_attempts,
        run_after=effective_run_after,
    )

    if bump_existing:
        stmt = stmt.on_conflict_do_update(
            index_elements=[Job.job_type, Job.dedupe_key],
            index_where=sa.text("status IN ('queued', 'running')"),
            set_={
                "priority": sa.func.least(Job.priority, stmt.excluded.priority),
                "run_after": sa.func.least(Job.run_after, stmt.excluded.run_after),
                "updated_at": sa.func.now(),
            },
            where=Job.status == "queued",
        )
    else:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[Job.job_type, Job.dedupe_key],
            index_where=sa.text("status IN ('queued', 'running')"),
        )

    result = await session.execute(stmt.returning(Job.id))
    return result.scalar_one_or_none()


async def claim_next_job(
    session: AsyncSession,
    *,
    worker_id: str,
    job_types: Sequence[str],
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    feed_id: uuid.UUID | None = None,
) -> Job | None:
    """Atomically claim the next ready job using FOR UPDATE SKIP LOCKED."""
    if not job_types:
        return None

    sql = sa.text(
        """
        WITH next_job AS (
            SELECT id FROM jobs
            WHERE status = 'queued'
              AND run_after <= now()
              AND job_type = ANY(:job_types)
              AND (CAST(:feed_id AS uuid) IS NULL OR feed_id = CAST(:feed_id AS uuid))
            ORDER BY priority ASC, run_after ASC, id ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE jobs j
        SET status = 'running',
            locked_by = :worker_id,
            locked_at = now(),
            started_at = now(),
            heartbeat_at = now(),
            lease_expires_at = now() + make_interval(secs => :lease_seconds),
            attempts = attempts + 1,
            updated_at = now()
        FROM next_job
        WHERE j.id = next_job.id
        RETURNING j.id
        """
    )
    params: dict[str, Any] = {
        "job_types": list(job_types),
        "worker_id": worker_id,
        "lease_seconds": lease_seconds,
        "feed_id": feed_id,
    }

    result = await session.execute(sql, params)
    job_id = result.scalar_one_or_none()
    if job_id is None:
        return None
    job = await session.get(Job, job_id, populate_existing=True)
    return job


async def heartbeat_job(
    session: AsyncSession,
    *,
    job_id: int,
    worker_id: str,
    claimed_attempt: int,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Renew lease for a running job under the full lease fence. Returns False if lease was lost."""
    result = cast(
        CursorResult[Any],
        await session.execute(
            sa.update(Job)
            .where(
                Job.id == job_id,
                Job.status == "running",
                Job.locked_by == worker_id,
                Job.attempts == claimed_attempt,
            )
            .values(
                heartbeat_at=sa.func.now(),
                lease_expires_at=sa.func.now()
                + sa.func.make_interval(0, 0, 0, 0, 0, 0, lease_seconds),
                updated_at=sa.func.now(),
            )
        ),
    )
    if (result.rowcount or 0) == 0:
        logger.warning(
            "job_lease_lost",
            extra={"job_id": job_id, "worker_id": worker_id, "attempt": claimed_attempt},
        )
        return False
    return True


async def complete_job(
    session: AsyncSession,
    *,
    job_id: int,
    job_type: str,
    worker_id: str,
    claimed_attempt: int,
    queue_wait_ms: int,
    duration_ms: int,
    metric_status: str = "ok",
    metric_outcome: str | None = None,
    http_status: int | None = None,
    feed_id: uuid.UUID | None = None,
    item_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
) -> bool:
    """Mark a job done under lease fence and record stage_metrics only if still lease owner."""
    clamped_wait = max(0, queue_wait_ms)
    clamped_duration = max(0, duration_ms)
    result = cast(
        CursorResult[Any],
        await session.execute(
            sa.update(Job)
            .where(
                Job.id == job_id,
                Job.status == "running",
                Job.locked_by == worker_id,
                Job.attempts == claimed_attempt,
            )
            .values(
                status="done",
                finished_at=sa.func.now(),
                queue_wait_ms=clamped_wait,
                duration_ms=clamped_duration,
                last_error=None,
                updated_at=sa.func.now(),
            )
        ),
    )
    if (result.rowcount or 0) == 0:
        logger.warning(
            "job_lease_lost",
            extra={"job_id": job_id, "worker_id": worker_id, "attempt": claimed_attempt},
        )
        return False

    session.add(
        StageMetric(
            run_id=run_id,
            job_type=job_type,
            feed_id=feed_id,
            item_id=item_id,
            status=metric_status,
            outcome=metric_outcome,
            http_status=http_status,
            queue_wait_ms=clamped_wait,
            duration_ms=clamped_duration,
        )
    )
    return True


async def fail_job(
    session: AsyncSession,
    *,
    job_id: int,
    job_type: str,
    worker_id: str,
    claimed_attempt: int,
    max_attempts: int,
    error: str,
    queue_wait_ms: int,
    duration_ms: int,
    permanent: bool = False,
    feed_id: uuid.UUID | None = None,
    item_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
) -> tuple[bool, bool]:
    """Fail a job under lease fence (retry or dead-letter). Returns (fenced_ok, dead_lettered)."""
    clamped_wait = max(0, queue_wait_ms)
    clamped_duration = max(0, duration_ms)
    dead_lettered = permanent or (claimed_attempt >= max_attempts)
    truncated_err = error[:2000]

    if dead_lettered:
        values: dict[str, Any] = {
            "status": "failed",
            "finished_at": sa.func.now(),
            "queue_wait_ms": clamped_wait,
            "duration_ms": clamped_duration,
            "last_error": truncated_err,
            "updated_at": sa.func.now(),
        }
    else:
        retry_delay_secs = int(compute_job_retry_delay(claimed_attempt).total_seconds())
        values = {
            "status": "queued",
            "locked_by": None,
            "locked_at": None,
            "started_at": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "run_after": sa.func.now() + sa.func.make_interval(0, 0, 0, 0, 0, 0, retry_delay_secs),
            "queue_wait_ms": clamped_wait,
            "duration_ms": clamped_duration,
            "last_error": truncated_err,
            "updated_at": sa.func.now(),
        }

    result = cast(
        CursorResult[Any],
        await session.execute(
            sa.update(Job)
            .where(
                Job.id == job_id,
                Job.status == "running",
                Job.locked_by == worker_id,
                Job.attempts == claimed_attempt,
            )
            .values(**values)
        ),
    )
    if (result.rowcount or 0) == 0:
        logger.warning(
            "job_lease_lost",
            extra={"job_id": job_id, "worker_id": worker_id, "attempt": claimed_attempt},
        )
        return False, False

    session.add(
        StageMetric(
            run_id=run_id,
            job_type=job_type,
            feed_id=feed_id,
            item_id=item_id,
            status="failed",
            queue_wait_ms=clamped_wait,
            duration_ms=clamped_duration,
        )
    )
    return True, dead_lettered


async def release_job_on_shutdown(
    session: AsyncSession,
    *,
    job_id: int,
    worker_id: str,
    claimed_attempt: int,
) -> bool:
    """Release an interrupted job back to queued on graceful shutdown under full lease fence."""
    result = cast(
        CursorResult[Any],
        await session.execute(
            sa.update(Job)
            .where(
                Job.id == job_id,
                Job.status == "running",
                Job.locked_by == worker_id,
                Job.attempts == claimed_attempt,
            )
            .values(
                status="queued",
                locked_by=None,
                locked_at=None,
                started_at=None,
                lease_expires_at=None,
                heartbeat_at=None,
                run_after=sa.func.now(),
                attempts=sa.func.greatest(Job.attempts - 1, 0),
                updated_at=sa.func.now(),
            )
        ),
    )
    return (result.rowcount or 0) > 0


async def reap_stale_leases(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    on_poll_feed_dead_letter: Callable[[uuid.UUID, str], Awaitable[object]] | None = None,
    on_extract_dead_letter: Callable[[uuid.UUID], Awaitable[None]] | None = None,
) -> ReapResult:
    """Reclaim expired running leases under LOCK_REAPER and invoke stage dead-letter hooks."""
    requeued = 0
    dead_lettered = 0
    dead_poll_feeds: list[uuid.UUID] = []
    dead_extract_items: list[uuid.UUID] = []

    async with session_factory() as session:
        locked = await try_advisory_xact_lock(session, LOCK_REAPER)
        if not locked:
            await session.rollback()
            return ReapResult(0, 0, (), ())

        rows = (
            (
                await session.execute(
                    sa.select(Job)
                    .where(Job.status == "running", Job.lease_expires_at < sa.func.now())
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )

        for job in rows:
            if job.attempts < job.max_attempts:
                delay_secs = int(compute_job_retry_delay(job.attempts).total_seconds())
                await session.execute(
                    sa.update(Job)
                    .where(Job.id == job.id)
                    .values(
                        status="queued",
                        locked_by=None,
                        locked_at=None,
                        started_at=None,
                        lease_expires_at=None,
                        heartbeat_at=None,
                        run_after=sa.func.now()
                        + sa.func.make_interval(0, 0, 0, 0, 0, 0, delay_secs),
                        last_error=LEASE_EXPIRED_ERROR,
                        updated_at=sa.func.now(),
                    )
                )
                requeued += 1
            else:
                await session.execute(
                    sa.update(Job)
                    .where(Job.id == job.id)
                    .values(
                        status="failed",
                        finished_at=sa.func.now(),
                        last_error=LEASE_EXPIRED_ERROR,
                        updated_at=sa.func.now(),
                    )
                )
                dead_lettered += 1
                if job.job_type == "poll_feed" and job.feed_id is not None:
                    dead_poll_feeds.append(job.feed_id)
                elif job.job_type == "extract_content" and job.item_id is not None:
                    dead_extract_items.append(job.item_id)

        await session.commit()

    # Invoke stage dead-letter hooks in fresh transactions
    if on_poll_feed_dead_letter is not None:
        for feed_id in dead_poll_feeds:
            await on_poll_feed_dead_letter(feed_id, LEASE_EXPIRED_ERROR)
    if on_extract_dead_letter is not None:
        for item_id in dead_extract_items:
            await on_extract_dead_letter(item_id)

    return ReapResult(
        requeued_count=requeued,
        dead_lettered_count=dead_lettered,
        dead_lettered_poll_feeds=tuple(dead_poll_feeds),
        dead_lettered_extract_items=tuple(dead_extract_items),
    )


async def retry_dead_letter_job(session: AsyncSession, job_id: int) -> Job | None:
    """Requeue a dead-letter job; raises ActiveJobCollisionError if an active duplicate exists."""
    job_meta = (
        await session.execute(
            sa.select(Job.id, Job.job_type, Job.dedupe_key).where(
                Job.id == job_id,
                Job.status == "failed",
            )
        )
    ).one_or_none()
    if job_meta is None:
        return None

    try:
        async with session.begin_nested():
            updated = (
                await session.execute(
                    sa.update(Job)
                    .where(Job.id == job_id, Job.status == "failed")
                    .values(
                        status="queued",
                        attempts=0,
                        run_after=sa.func.now(),
                        locked_by=None,
                        locked_at=None,
                        started_at=None,
                        lease_expires_at=None,
                        heartbeat_at=None,
                        finished_at=None,
                        last_error=None,
                        updated_at=sa.func.now(),
                    )
                    .returning(Job)
                )
            ).scalar_one_or_none()
            await session.flush()
    except IntegrityError as exc:
        active_id = (
            await session.execute(
                sa.select(Job.id).where(
                    Job.job_type == job_meta.job_type,
                    Job.dedupe_key == job_meta.dedupe_key,
                    Job.status.in_(("queued", "running")),
                )
            )
        ).scalar_one_or_none()
        raise ActiveJobCollisionError(active_id) from exc

    return updated


async def dismiss_dead_letter_job(session: AsyncSession, job_id: int) -> bool:
    """Delete an acknowledged dead-letter job."""
    result = cast(
        CursorResult[Any],
        await session.execute(sa.delete(Job).where(Job.id == job_id, Job.status == "failed")),
    )
    return (result.rowcount or 0) > 0


def compute_queue_wait_ms(job: Job, now: datetime | None = None) -> int:
    """Compute non-negative queue wait duration in milliseconds."""
    started = job.started_at or now or datetime.now(UTC)
    delta: timedelta = started - job.run_after
    return max(0, int(delta.total_seconds() * 1000))
