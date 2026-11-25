"""Advisory-locked periodic scheduler and stage job dispatcher (§8.2, §8.3)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from smartrss.db.models import AppSettings, Feed
from smartrss.jobs.locks import LOCK_SCHEDULER, try_advisory_xact_lock
from smartrss.jobs.queue import enqueue_job

RETENTION_INTERVAL = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    poll_jobs_enqueued: int = 0
    expire_enqueued: bool = False
    retention_enqueued: bool = False


async def is_retention_cleanup_due(session: AsyncSession) -> bool:
    """Return True if retention_cleanup has never run or last ran > 24h ago."""
    settings_row = await session.get(AppSettings, 1)
    if settings_row is None or settings_row.last_retention_cleanup_at is None:
        return True
    return settings_row.last_retention_cleanup_at < (datetime.now(UTC) - RETENTION_INTERVAL)


async def dispatch_due_jobs(
    session: AsyncSession,
    *,
    job_types: Sequence[str],
    feed_id: uuid.UUID | None = None,
    force_poll: bool = False,
    run_id: uuid.UUID | None = None,
    force_maintenance: bool = False,
) -> DispatchResult:
    """Enqueue due jobs for the requested job types."""
    poll_enqueued = 0
    expire_enqueued = False
    retention_enqueued = False

    if "poll_feed" in job_types:
        stmt = sa.select(Feed.id).where(
            Feed.status.in_(("active", "muted")),
            Feed.health_state != "gone",
        )
        if feed_id is not None:
            stmt = stmt.where(Feed.id == feed_id)
        if not force_poll:
            stmt = stmt.where(
                sa.or_(Feed.next_fetch_at.is_(None), Feed.next_fetch_at <= sa.func.now())
            )
        due_feed_ids = (await session.execute(stmt)).scalars().all()
        for fid in due_feed_ids:
            jid = await enqueue_job(
                session,
                job_type="poll_feed",
                dedupe_key=f"feed:{fid}",
                feed_id=fid,
                run_id=run_id,
                priority=50,
                bump_existing=force_poll,
            )
            if jid is not None:
                poll_enqueued += 1

    if "expire_items" in job_types and feed_id is None:
        jid = await enqueue_job(
            session,
            job_type="expire_items",
            dedupe_key="global:expire_items",
            run_id=run_id,
            priority=60,
        )
        expire_enqueued = jid is not None

    if (
        "retention_cleanup" in job_types
        and feed_id is None
        and (force_maintenance or await is_retention_cleanup_due(session))
    ):
        jid = await enqueue_job(
            session,
            job_type="retention_cleanup",
            dedupe_key="global:retention_cleanup",
            run_id=run_id,
            priority=70,
        )
        retention_enqueued = jid is not None

    return DispatchResult(
        poll_jobs_enqueued=poll_enqueued,
        expire_enqueued=expire_enqueued,
        retention_enqueued=retention_enqueued,
    )


async def run_scheduler_tick(
    session: AsyncSession,
    *,
    include_hourly_expire: bool,
) -> DispatchResult | None:
    """Execute one advisory-locked daemon scheduler tick. Returns None if lock not acquired."""
    acquired = await try_advisory_xact_lock(session, LOCK_SCHEDULER)
    if not acquired:
        return None

    types = ["poll_feed", "retention_cleanup"]
    if include_hourly_expire:
        types.append("expire_items")
    return await dispatch_due_jobs(session, job_types=types)
