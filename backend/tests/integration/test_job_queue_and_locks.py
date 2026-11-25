"""Integration tests for SKIP LOCKED job queue, lease fencing, reaper, dead-letter, and locks."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from smartrss.db.models import Feed, Item, Job, StageMetric
from smartrss.db.sync_log import SyncEvent, sync_log
from smartrss.jobs.locks import (
    LOCK_RUN_ONCE,
    LOCK_SCHEDULER,
    advisory_session_lock,
    try_advisory_xact_lock,
)
from smartrss.jobs.queue import (
    claim_next_job,
    complete_job,
    enqueue_job,
    fail_job,
    heartbeat_job,
    reap_stale_leases,
    release_job_on_shutdown,
)
from smartrss.worker.stages.extract_content import mark_extract_content_failed
from smartrss.worker.stages.poll_feed import record_poll_feed_unexpected_failure


async def test_skip_locked_concurrent_claims_and_dedupe_bump(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    async with session_factory() as s:
        j1 = await enqueue_job(
            s,
            job_type="poll_feed",
            dedupe_key="feed:bump-test",
            priority=50,
            run_after=future,
        )
        assert j1 is not None
        # Default enqueue ignores duplicate
        j_dup = await enqueue_job(s, job_type="poll_feed", dedupe_key="feed:bump-test", priority=10)
        assert j_dup is None

        # bump_existing wakes backed-off queued job to priority=10 and run_after<=now
        j_bumped = await enqueue_job(
            s,
            job_type="poll_feed",
            dedupe_key="feed:bump-test",
            priority=10,
            bump_existing=True,
        )
        assert j_bumped == j1

        # Enqueue 5 more distinct jobs
        for i in range(5):
            await enqueue_job(s, job_type="poll_feed", dedupe_key=f"feed:k{i}", priority=50)
        await s.commit()

    async def claim_worker(wid: str) -> int | None:
        async with session_factory() as s:
            job = await claim_next_job(s, worker_id=wid, job_types=["poll_feed"])
            await s.commit()
            return job.id if job else None

    claimed = await asyncio.gather(*(claim_worker(f"w{i}") for i in range(8)))
    non_none = [c for c in claimed if c is not None]
    assert len(non_none) == 6
    assert len(set(non_none)) == 6  # zero double claims


async def test_fenced_heartbeat_complete_fail_and_reaper_hooks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Stale worker losing its lease cannot clobber attempt 2 or write stage_metrics."""
    feed_id = uuid.uuid4()
    item_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with session_factory() as s:
        s.add(Feed(id=feed_id, feed_url="https://lease.example.com/rss", title="Lease Feed"))
        await s.flush()
        s.add(
            Item(
                id=item_id,
                feed_id=feed_id,
                guid="g1",
                raw_url="https://lease.example.com/1",
                canonical_url="https://lease.example.com/1",
                title="Item 1",
                fetched_at=now,
                read_state_updated_at=now,
                starred_updated_at=now,
            )
        )
        await s.flush()
        await sync_log.record(
            s, [SyncEvent("feed", feed_id, "upsert"), SyncEvent("item", item_id, "upsert")]
        )
        jid = await enqueue_job(
            s, job_type="poll_feed", dedupe_key=f"feed:{feed_id}", feed_id=feed_id, max_attempts=2
        )
        assert jid is not None
        await s.commit()

    # Worker A claims attempt 1 and renews heartbeat
    async with session_factory() as s:
        job_a = await claim_next_job(s, worker_id="worker-a", job_types=["poll_feed"])
        assert job_a is not None
        assert job_a.attempts == 1
        assert await heartbeat_job(s, job_id=job_a.id, worker_id="worker-a", claimed_attempt=1)
        # Simulate stale lease expiry
        await s.execute(
            sa.update(Job)
            .where(Job.id == job_a.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=10))
        )
        await s.commit()

    # Reaper reclaims stale lease back to queued (with backoff)
    reap1 = await reap_stale_leases(session_factory)
    assert reap1.requeued_count == 1
    assert reap1.dead_lettered_count == 0

    # Make it immediately ready and let Worker B claim attempt 2
    async with session_factory() as s:
        await s.execute(
            sa.update(Job)
            .where(Job.id == job_a.id)
            .values(run_after=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()

    async with session_factory() as s:
        job_b = await claim_next_job(s, worker_id="worker-b", job_types=["poll_feed"])
        assert job_b is not None
        assert job_b.id == job_a.id
        assert job_b.attempts == 2
        await s.commit()

    # Stale Worker A tries heartbeat, complete, and fail -> ALL fenced out (return False, 0 metrics)
    async with session_factory() as s:
        assert not await heartbeat_job(s, job_id=job_a.id, worker_id="worker-a", claimed_attempt=1)
        assert not await complete_job(
            s,
            job_id=job_a.id,
            job_type="poll_feed",
            worker_id="worker-a",
            claimed_attempt=1,
            queue_wait_ms=10,
            duration_ms=50,
            feed_id=feed_id,
        )
        fenced_ok, _ = await fail_job(
            s,
            job_id=job_a.id,
            job_type="poll_feed",
            worker_id="worker-a",
            claimed_attempt=1,
            max_attempts=2,
            error="stale err",
            queue_wait_ms=10,
            duration_ms=50,
        )
        assert not fenced_ok
        await s.commit()

    async with session_factory() as s:
        metric_count = (
            await s.execute(sa.select(sa.func.count()).select_from(StageMetric))
        ).scalar_one()
        assert metric_count == 0
        # Expire Worker B's lease (attempt 2 == max_attempts 2) -> reaper dead-letters & runs hook
        await s.execute(
            sa.update(Job)
            .where(Job.id == job_b.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=5))
        )
        # Also create an expired final-attempt extract_content job
        ejid = await enqueue_job(
            s,
            job_type="extract_content",
            dedupe_key=f"item:{item_id}",
            feed_id=feed_id,
            item_id=item_id,
            max_attempts=1,
        )
        assert ejid is not None
        ejob = await claim_next_job(s, worker_id="worker-c", job_types=["extract_content"])
        assert ejob is not None
        await s.execute(
            sa.update(Job)
            .where(Job.id == ejob.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=5))
        )
        await s.commit()

    reap2 = await reap_stale_leases(
        session_factory,
        on_poll_feed_dead_letter=lambda fid, err: record_poll_feed_unexpected_failure(
            session_factory, fid, err
        ),
        on_extract_dead_letter=lambda iid: mark_extract_content_failed(session_factory, iid),
    )
    assert reap2.dead_lettered_count == 2

    async with session_factory() as s:
        feed = await s.get(Feed, feed_id)
        item = await s.get(Item, item_id)
        assert feed is not None and feed.health_state == "degraded" and feed.consecutive_errors == 1
        assert feed.next_fetch_at is not None and feed.next_fetch_at > now
        assert item is not None and item.extraction_status == "failed"


async def test_dead_letter_api_retry_collision_and_dismiss(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client: httpx.AsyncClient = api_env["client"]

    async with session_factory() as s:
        jid = await enqueue_job(s, job_type="poll_feed", dedupe_key="feed:dl-1", max_attempts=1)
        assert jid is not None
        job = await claim_next_job(s, worker_id="w1", job_types=["poll_feed"])
        assert job is not None
        fenced, dl = await fail_job(
            s,
            job_id=job.id,
            job_type="poll_feed",
            worker_id="w1",
            claimed_attempt=1,
            max_attempts=1,
            error="boom",
            queue_wait_ms=5,
            duration_ms=20,
        )
        assert fenced and dl
        # Enqueue an active duplicate so retry triggers 409 Conflict
        active_id = await enqueue_job(s, job_type="poll_feed", dedupe_key="feed:dl-1")
        assert active_id is not None
        await s.commit()

    r_list = await client.get("/api/v1/system/jobs/dead-letter")
    assert r_list.status_code == 200
    assert r_list.json()["total"] == 1

    r_409 = await client.post(f"/api/v1/system/jobs/{jid}/retry")
    assert r_409.status_code == 409
    assert r_409.json()["detail"]["active_job_id"] == active_id

    # Complete active job so retry succeeds
    async with session_factory() as s:
        await s.execute(sa.update(Job).where(Job.id == active_id).values(status="done"))
        await s.commit()

    r_retry = await client.post(f"/api/v1/system/jobs/{jid}/retry")
    assert r_retry.status_code == 200
    assert r_retry.json()["status"] == "queued"
    assert r_retry.json()["attempts"] == 0

    # Test shutdown release & dismiss
    async with session_factory() as s:
        claimed = await claim_next_job(s, worker_id="w-shut", job_types=["poll_feed"])
        assert claimed is not None
        released = await release_job_on_shutdown(
            s, job_id=claimed.id, worker_id="w-shut", claimed_attempt=1
        )
        assert released is True
        # Now fail it permanently and dismiss via API
        claimed2 = await claim_next_job(s, worker_id="w-shut", job_types=["poll_feed"])
        assert claimed2 is not None
        await fail_job(
            s,
            job_id=claimed2.id,
            job_type="poll_feed",
            worker_id="w-shut",
            claimed_attempt=claimed2.attempts,
            max_attempts=5,
            error="fatal",
            permanent=True,
            queue_wait_ms=1,
            duration_ms=2,
        )
        await s.commit()

    r_dismiss = await client.post(f"/api/v1/system/jobs/{jid}/dismiss")
    assert r_dismiss.status_code == 204


async def test_advisory_locks_pool_safety(
    db_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # 1. Session advisory lock on dedicated connection blocks second acquirer and releases cleanly
    async with advisory_session_lock(db_engine, LOCK_RUN_ONCE) as first:
        assert first is True
        async with advisory_session_lock(db_engine, LOCK_RUN_ONCE) as second:
            assert second is False

    # Re-acquiring after exit succeeds (no leaked lock on pooled connection)
    async with advisory_session_lock(db_engine, LOCK_RUN_ONCE) as third:
        assert third is True

    # 2. Transaction advisory lock blocks concurrent tx and releases on commit
    async with session_factory() as s1, session_factory() as s2:
        assert await try_advisory_xact_lock(s1, LOCK_SCHEDULER) is True
        assert await try_advisory_xact_lock(s2, LOCK_SCHEDULER) is False
        await s1.commit()
        assert await try_advisory_xact_lock(s2, LOCK_SCHEDULER) is True
        await s2.commit()

    # 3. I-16 regression: unlock failure or cancellation invalidates the connection
    invalidated = False

    class _FakeUnlockFailConn:
        def __init__(self, exc: BaseException) -> None:
            self._exc = exc
            self._calls = 0

        async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
            self._calls += 1
            if self._calls == 1:

                class _Res:
                    def scalar_one(self) -> bool:
                        return True

                return _Res()
            raise self._exc

        async def invalidate(self) -> None:
            nonlocal invalidated
            invalidated = True

        async def __aenter__(self) -> _FakeUnlockFailConn:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    class _FakeEngine:
        def __init__(self, exc: BaseException) -> None:
            self._exc = exc

        def connect(self) -> _FakeUnlockFailConn:
            return _FakeUnlockFailConn(self._exc)

    # Normal Exception on unlock -> invalidated without raising
    async with advisory_session_lock(
        _FakeEngine(RuntimeError("conn dropped")), LOCK_RUN_ONCE
    ) as ok:  # type: ignore[arg-type]
        assert ok is True
    assert invalidated is True

    # CancelledError on unlock -> invalidated AND re-raised
    invalidated = False
    try:
        async with advisory_session_lock(
            _FakeEngine(asyncio.CancelledError()),
            LOCK_RUN_ONCE,  # type: ignore[arg-type]
        ) as ok:
            assert ok is True
    except asyncio.CancelledError:
        pass
    assert invalidated is True
