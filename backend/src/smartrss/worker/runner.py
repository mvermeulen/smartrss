"""Worker execution modes: continuous daemon and cron-friendly one-shot batch mode (§8.3)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import time
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from smartrss.db.models import Job
from smartrss.jobs.locks import LOCK_RUN_ONCE, advisory_session_lock
from smartrss.jobs.metrics import create_pipeline_run, finalize_pipeline_run
from smartrss.jobs.queue import (
    DEFAULT_LEASE_SECONDS,
    claim_next_job,
    complete_job,
    compute_queue_wait_ms,
    enqueue_job,
    fail_job,
    heartbeat_job,
    reap_stale_leases,
    release_job_on_shutdown,
)
from smartrss.security.safe_http import DomainRateLimiter, SafeHttpClient
from smartrss.worker.scheduler import dispatch_due_jobs, run_scheduler_tick
from smartrss.worker.stages.expire_items import run_expire_items
from smartrss.worker.stages.extract_content import (
    TransientExtractionError,
    mark_extract_content_failed,
    run_extract_content,
)
from smartrss.worker.stages.maintenance import run_retention_cleanup
from smartrss.worker.stages.poll_feed import (
    PollFeedUnexpectedError,
    record_poll_feed_unexpected_failure,
    run_poll_feed,
)

logger = logging.getLogger(__name__)

ALL_1A_JOB_TYPES: tuple[str, ...] = (
    "poll_feed",
    "extract_content",
    "expire_items",
    "retention_cleanup",
)

STAGE_TO_JOB_TYPES: dict[str, tuple[str, ...]] = {
    "all": ALL_1A_JOB_TYPES,
    "poll": ("poll_feed",),
    "extract": ("extract_content",),
    "expire": ("expire_items",),
    "maintenance": ("retention_cleanup",),
}

HEARTBEAT_INTERVAL_SECONDS = 30.0
REAPER_INTERVAL_SECONDS = 60.0
EXPIRE_DISPATCH_INTERVAL_SECONDS = 3600.0
SHUTDOWN_GRACE_SECONDS = 30.0


@dataclass(slots=True)
class _RunCounters:
    items_polled: int = 0
    items_extracted: int = 0
    jobs_failed: int = 0


@dataclass(frozen=True, slots=True)
class RunOnceSummary:
    status: str  # "completed" | "partial" | "failed" | "skipped_locked"
    exit_code: int
    run_id: str | None = None
    stages: list[str] | None = None
    duration_ms: int = 0
    items_polled: int = 0
    items_extracted: int = 0
    jobs_failed: int = 0
    jobs_deferred: int = 0
    reaped_requeued: int = 0
    reaped_dead_lettered: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def make_worker_id(prefix: str = "worker") -> str:
    return f"{prefix}:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


async def _heartbeat_loop(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: int,
    worker_id: str,
    claimed_attempt: int,
    interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> None:
    """Periodically renew job lease; survives transient DB errors until lease is lost (I-02)."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            async with session_factory() as session:
                ok = await heartbeat_job(
                    session,
                    job_id=job_id,
                    worker_id=worker_id,
                    claimed_attempt=claimed_attempt,
                    lease_seconds=lease_seconds,
                )
                await session.commit()
                if not ok:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Heartbeat renewal failed; will retry",
                extra={"job_id": job_id, "worker_id": worker_id},
            )


async def execute_claimed_job(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: SafeHttpClient,
    rate_limiter: DomainRateLimiter,
    *,
    job: Job,
    worker_id: str,
    counters: _RunCounters | None = None,
    heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> None:
    """Execute a single claimed job with heartbeat renewal and lease-fenced completion/failure."""
    claimed_attempt = job.attempts
    queue_wait_ms = compute_queue_wait_ms(job)
    started_monotonic = time.monotonic()

    hb_task = asyncio.create_task(
        _heartbeat_loop(
            session_factory,
            job_id=job.id,
            worker_id=worker_id,
            claimed_attempt=claimed_attempt,
            interval_seconds=heartbeat_interval,
            lease_seconds=lease_seconds,
        )
    )

    try:
        metric_status = "ok"
        repoll_feed_id: uuid.UUID | None = None
        if job.job_type == "poll_feed":
            assert job.feed_id is not None
            outcome = await run_poll_feed(
                session_factory,
                http_client,
                feed_id=job.feed_id,
                run_id=job.run_id,
            )
            metric_status = outcome.metric_status
            if outcome.repoll_needed:
                repoll_feed_id = job.feed_id
            if counters is not None:
                counters.items_polled += outcome.items_ingested
        elif job.job_type == "extract_content":
            assert job.item_id is not None
            ext_outcome = await run_extract_content(
                session_factory,
                http_client,
                rate_limiter,
                item_id=job.item_id,
            )
            metric_status = ext_outcome.metric_status
            if counters is not None and ext_outcome.extraction_status == "ok":
                counters.items_extracted += 1
        elif job.job_type == "expire_items":
            await run_expire_items(session_factory)
        elif job.job_type == "retention_cleanup":
            async with session_factory() as session:
                await run_retention_cleanup(session)
                await session.commit()
        else:
            raise ValueError(f"Unknown job_type: {job.job_type!r}")

        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        async with session_factory() as session:
            completed = await complete_job(
                session,
                job_id=job.id,
                job_type=job.job_type,
                worker_id=worker_id,
                claimed_attempt=claimed_attempt,
                queue_wait_ms=queue_wait_ms,
                duration_ms=duration_ms,
                metric_status=metric_status,
                feed_id=job.feed_id,
                item_id=job.item_id,
                run_id=job.run_id,
            )
            if completed and repoll_feed_id is not None:
                # I-04: Re-enqueue poll for newly edited feed_url after closing running job
                await enqueue_job(
                    session,
                    job_type="poll_feed",
                    dedupe_key=f"feed:{repoll_feed_id}",
                    feed_id=repoll_feed_id,
                    run_id=job.run_id,
                    priority=10,
                    bump_existing=True,
                )
            await session.commit()
    except asyncio.CancelledError:
        raise
    except TransientExtractionError as exc:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        if counters is not None:
            counters.jobs_failed += 1
        async with session_factory() as session:
            fenced_ok, dead_lettered = await fail_job(
                session,
                job_id=job.id,
                job_type=job.job_type,
                worker_id=worker_id,
                claimed_attempt=claimed_attempt,
                max_attempts=job.max_attempts,
                error=str(exc),
                queue_wait_ms=queue_wait_ms,
                duration_ms=duration_ms,
                feed_id=job.feed_id,
                item_id=job.item_id,
                run_id=job.run_id,
            )
            await session.commit()
        if fenced_ok and dead_lettered and job.item_id is not None:
            await mark_extract_content_failed(session_factory, job.item_id)
    except Exception as exc:
        logger.exception("Unhandled error in job %s (%s)", job.id, job.job_type)
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        if counters is not None:
            counters.jobs_failed += 1
        poll_url: str | None = None
        if isinstance(exc, PollFeedUnexpectedError):
            poll_url = exc.feed_url
            err_str = str(exc)
        else:
            err_str = f"{type(exc).__name__}: {exc}"
        async with session_factory() as session:
            fenced_ok, dead_lettered = await fail_job(
                session,
                job_id=job.id,
                job_type=job.job_type,
                worker_id=worker_id,
                claimed_attempt=claimed_attempt,
                max_attempts=job.max_attempts,
                error=err_str,
                queue_wait_ms=queue_wait_ms,
                duration_ms=duration_ms,
                feed_id=job.feed_id,
                item_id=job.item_id,
                run_id=job.run_id,
            )
            await session.commit()
        if fenced_ok:
            if job.job_type == "poll_feed" and job.feed_id is not None:
                repoll = await record_poll_feed_unexpected_failure(
                    session_factory, job.feed_id, err_str, expected_feed_url=poll_url
                )
                if repoll:
                    # I-04: feed_url was edited mid-poll; poll the new URL promptly
                    async with session_factory() as session:
                        await enqueue_job(
                            session,
                            job_type="poll_feed",
                            dedupe_key=f"feed:{job.feed_id}",
                            feed_id=job.feed_id,
                            run_id=job.run_id,
                            priority=10,
                            bump_existing=True,
                        )
                        await session.commit()
            elif job.job_type == "extract_content" and dead_lettered and job.item_id is not None:
                await mark_extract_content_failed(session_factory, job.item_id)
    finally:
        hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await hb_task


async def _count_ready_running_and_deferred(
    session: AsyncSession,
    *,
    job_types: Sequence[str],
    worker_prefix: str,
    feed_id: uuid.UUID | None = None,
) -> tuple[int, int]:
    """Return (ready_or_own_running, deferred_count) scoped to this invocation (I-03).

    Foreign running jobs (e.g. unexpired leases from a killed previous run) are counted
    as deferred rather than spinning this run until --max-duration.
    """
    base_conds: list[sa.ColumnElement[bool]] = [Job.job_type.in_(job_types)]
    if feed_id is not None:
        base_conds.append(Job.feed_id == feed_id)

    own_like = f"{worker_prefix}:%"

    ready_or_own_running = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(Job)
            .where(
                *base_conds,
                sa.or_(
                    sa.and_(Job.status == "running", Job.locked_by.like(own_like)),
                    sa.and_(Job.status == "queued", Job.run_after <= sa.func.now()),
                ),
            )
        )
    ).scalar_one()

    deferred = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(Job)
            .where(
                *base_conds,
                sa.or_(
                    sa.and_(Job.status == "queued", Job.run_after > sa.func.now()),
                    sa.and_(
                        Job.status == "running",
                        sa.or_(
                            Job.locked_by.is_(None),
                            sa.not_(Job.locked_by.like(own_like)),
                        ),
                    ),
                ),
            )
        )
    ).scalar_one()

    return int(ready_or_own_running), int(deferred)


async def run_worker_once(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    http_client: SafeHttpClient,
    *,
    stage: str = "all",
    feed_id: uuid.UUID | None = None,
    force_poll: bool = False,
    max_duration_seconds: float = 1800.0,
    concurrency: int = 4,
    rate_limiter: DomainRateLimiter | None = None,
    install_signals: bool = True,
) -> RunOnceSummary:
    """Execute a single cron-friendly batch run under LOCK_RUN_ONCE (§8.3)."""
    if stage not in STAGE_TO_JOB_TYPES:
        raise ValueError(f"Invalid stage: {stage!r}")

    job_types = STAGE_TO_JOB_TYPES[stage]
    worker_prefix = make_worker_id("run-once")
    limiter = rate_limiter or DomainRateLimiter()

    async with advisory_session_lock(engine, LOCK_RUN_ONCE) as acquired:
        if not acquired:
            logger.info("worker_run_once_skipped_locked")
            return RunOnceSummary(status="skipped_locked", exit_code=0, stages=list(job_types))

        started = time.monotonic()

        # 1. Always run stale-lease reaper first
        reap_res = await reap_stale_leases(
            session_factory,
            on_poll_feed_dead_letter=lambda fid, err: record_poll_feed_unexpected_failure(
                session_factory, fid, err
            ),
            on_extract_dead_letter=lambda iid: mark_extract_content_failed(session_factory, iid),
        )

        # 2. Create pipeline_runs row & dispatch due jobs
        async with session_factory() as session:
            run = await create_pipeline_run(session, trigger="cron_cli", stages=job_types)
            run_id = run.id
            await dispatch_due_jobs(
                session,
                job_types=job_types,
                feed_id=feed_id,
                force_poll=force_poll,
                run_id=run_id,
                force_maintenance=(stage == "maintenance"),
            )
            await session.commit()

        counters = _RunCounters()
        timed_out = False
        cancelled_by_signal = False
        deadline = started + max_duration_seconds
        work_changed = asyncio.Event()
        active_slots = 0

        async def _worker_loop(slot_idx: int) -> None:
            nonlocal timed_out, active_slots
            slot_worker_id = f"{worker_prefix}:{slot_idx}"
            while True:
                if time.monotonic() >= deadline:
                    timed_out = True
                    work_changed.set()
                    return

                async with session_factory() as session:
                    job = await claim_next_job(
                        session,
                        worker_id=slot_worker_id,
                        job_types=job_types,
                        feed_id=feed_id,
                    )
                    await session.commit()

                if job is None:
                    # Wait for sibling slots to finish (may enqueue follow-up jobs) via Event
                    work_changed.clear()
                    async with session_factory() as session:
                        ready_or_own, _ = await _count_ready_running_and_deferred(
                            session,
                            job_types=job_types,
                            worker_prefix=worker_prefix,
                            feed_id=feed_id,
                        )
                    if ready_or_own == 0 and active_slots == 0:
                        return
                    wait_budget = min(0.5, max(0.05, deadline - time.monotonic()))
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(work_changed.wait(), timeout=wait_budget)
                    continue

                active_slots += 1
                try:
                    await execute_claimed_job(
                        session_factory,
                        http_client,
                        limiter,
                        job=job,
                        worker_id=slot_worker_id,
                        counters=counters,
                    )
                except asyncio.CancelledError:
                    async with session_factory() as session:
                        await release_job_on_shutdown(
                            session,
                            job_id=job.id,
                            worker_id=slot_worker_id,
                            claimed_attempt=job.attempts,
                        )
                        await session.commit()
                    raise
                finally:
                    active_slots -= 1
                    work_changed.set()

        worker_tasks = [asyncio.create_task(_worker_loop(i)) for i in range(max(1, concurrency))]

        # I-03: Install SIGTERM/SIGINT handler so worker tasks cancel & release leases cleanly
        loop = asyncio.get_running_loop()
        installed_sigs: list[signal.Signals] = []
        if install_signals:

            def _cancel_workers() -> None:
                nonlocal cancelled_by_signal
                cancelled_by_signal = True
                for task in worker_tasks:
                    task.cancel()

            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError, RuntimeError):
                    loop.add_signal_handler(sig, _cancel_workers)
                    installed_sigs.append(sig)

        try:
            remaining = max(0.1, deadline - time.monotonic())
            done, pending = await asyncio.wait(worker_tasks, timeout=remaining)
            if pending:
                timed_out = True
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    raise exc
        except Exception as exc:
            async with session_factory() as session:
                await finalize_pipeline_run(
                    session,
                    run_id=run_id,
                    status="failed",
                    items_polled=counters.items_polled,
                    items_extracted=counters.items_extracted,
                    jobs_failed=counters.jobs_failed,
                    notes=f"Fatal error: {type(exc).__name__}: {exc}",
                )
                await session.commit()
            return RunOnceSummary(
                status="failed",
                exit_code=1,
                run_id=str(run_id),
                stages=list(job_types),
                duration_ms=int((time.monotonic() - started) * 1000),
                items_polled=counters.items_polled,
                items_extracted=counters.items_extracted,
                jobs_failed=counters.jobs_failed,
                reaped_requeued=reap_res.requeued_count,
                reaped_dead_lettered=reap_res.dead_lettered_count,
            )
        finally:
            for sig in installed_sigs:
                loop.remove_signal_handler(sig)

        async with session_factory() as session:
            ready_or_own, deferred_count = await _count_ready_running_and_deferred(
                session,
                job_types=job_types,
                worker_prefix=worker_prefix,
                feed_id=feed_id,
            )
            is_partial = (
                timed_out or cancelled_by_signal or ready_or_own > 0 or counters.jobs_failed > 0
            )
            final_status = "partial" if is_partial else "completed"
            notes_parts: list[str] = []
            if deferred_count > 0:
                notes_parts.append(f"jobs_deferred={deferred_count}")
            if timed_out:
                notes_parts.append("max_duration_reached")
            if cancelled_by_signal:
                notes_parts.append("cancelled_by_signal")
            notes = "; ".join(notes_parts) if notes_parts else None

            finalized = await finalize_pipeline_run(
                session,
                run_id=run_id,
                status=final_status,
                items_polled=counters.items_polled,
                items_extracted=counters.items_extracted,
                jobs_failed=counters.jobs_failed,
                notes=notes,
            )
            await session.commit()

        return RunOnceSummary(
            status=final_status,
            exit_code=2 if final_status == "partial" else 0,
            run_id=str(run_id),
            stages=list(job_types),
            duration_ms=finalized.duration_ms or 0,
            items_polled=counters.items_polled,
            items_extracted=counters.items_extracted,
            jobs_failed=counters.jobs_failed,
            jobs_deferred=deferred_count,
            reaped_requeued=reap_res.requeued_count,
            reaped_dead_lettered=reap_res.dead_lettered_count,
        )


async def run_worker_daemon(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: SafeHttpClient,
    *,
    concurrency: int = 4,
    poll_interval_seconds: float = 30.0,
    shutdown_event: asyncio.Event | None = None,
    shutdown_grace_seconds: float = SHUTDOWN_GRACE_SECONDS,
    heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    idle_poll_seconds: float = 1.0,
) -> None:
    """Run continuous daemon supervisor with scheduler, reaper, I/O pool, and graceful shutdown."""
    stop = shutdown_event or asyncio.Event()
    worker_prefix = make_worker_id("daemon")
    rate_limiter = DomainRateLimiter()
    in_flight: dict[str, tuple[int, int]] = {}  # slot_worker_id -> (job_id, attempt)

    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    if shutdown_event is None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, stop.set)
                installed_signals.append(sig)

    async def _scheduler_loop() -> None:
        last_expire_dispatch = 0.0
        while not stop.is_set():
            now_mono = time.monotonic()
            include_expire = (now_mono - last_expire_dispatch) >= EXPIRE_DISPATCH_INTERVAL_SECONDS
            try:
                async with session_factory() as session:
                    res = await run_scheduler_tick(session, include_hourly_expire=include_expire)
                    await session.commit()
                if res is not None and include_expire:
                    last_expire_dispatch = now_mono
            except Exception:
                logger.exception("Scheduler tick error")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_interval_seconds)

    async def _reaper_loop() -> None:
        while not stop.is_set():
            try:
                await reap_stale_leases(
                    session_factory,
                    on_poll_feed_dead_letter=lambda fid, err: record_poll_feed_unexpected_failure(
                        session_factory, fid, err
                    ),
                    on_extract_dead_letter=lambda iid: mark_extract_content_failed(
                        session_factory, iid
                    ),
                )
            except Exception:
                logger.exception("Reaper pass error")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=REAPER_INTERVAL_SECONDS)

    async def _io_worker_loop(slot_idx: int) -> None:
        slot_worker_id = f"{worker_prefix}:{slot_idx}"
        while not stop.is_set():
            job: Job | None = None
            try:
                async with session_factory() as session:
                    job = await claim_next_job(
                        session,
                        worker_id=slot_worker_id,
                        job_types=ALL_1A_JOB_TYPES,
                    )
                    await session.commit()
            except Exception:
                logger.exception("Error claiming job")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=idle_poll_seconds)
                continue

            if job is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=idle_poll_seconds)
                continue

            in_flight[slot_worker_id] = (job.id, job.attempts)
            try:
                await execute_claimed_job(
                    session_factory,
                    http_client,
                    rate_limiter,
                    job=job,
                    worker_id=slot_worker_id,
                    heartbeat_interval=heartbeat_interval_seconds,
                )
            except asyncio.CancelledError:
                async with session_factory() as session:
                    await release_job_on_shutdown(
                        session,
                        job_id=job.id,
                        worker_id=slot_worker_id,
                        claimed_attempt=job.attempts,
                    )
                    await session.commit()
                raise
            except Exception:
                # I-02: Survive transient DB errors in heartbeat/completion without killing slot
                logger.exception(
                    "Transient error executing job %s in slot %s; slot continuing",
                    job.id,
                    slot_worker_id,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=idle_poll_seconds)
            finally:
                in_flight.pop(slot_worker_id, None)

    sched_task = asyncio.create_task(_scheduler_loop())
    reaper_task = asyncio.create_task(_reaper_loop())
    io_tasks = [asyncio.create_task(_io_worker_loop(i)) for i in range(max(1, concurrency))]
    stop_task = asyncio.create_task(stop.wait())

    all_io_dead = False
    try:
        while not stop.is_set():
            wait_set: set[asyncio.Task[object]] = {stop_task}
            alive_io = [t for t in io_tasks if not t.done()]
            if not alive_io:
                all_io_dead = True
                break
            wait_set.update(alive_io)
            await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

        logger.info("Worker daemon shutting down (grace=%ss)", shutdown_grace_seconds)
        sched_task.cancel()
        reaper_task.cancel()
        await asyncio.gather(sched_task, reaper_task, return_exceptions=True)

        # Wait up to shutdown_grace_seconds for in-flight I/O jobs to finish cleanly
        _done, pending = await asyncio.wait(io_tasks, timeout=shutdown_grace_seconds)
        if pending:
            logger.warning(
                "Cancelling %d in-flight worker task(s) after grace timeout", len(pending)
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        stop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task
        for sig in installed_signals:
            loop.remove_signal_handler(sig)
        for slot_worker_id, (job_id, attempt) in list(in_flight.items()):
            try:
                async with session_factory() as session:
                    await release_job_on_shutdown(
                        session,
                        job_id=job_id,
                        worker_id=slot_worker_id,
                        claimed_attempt=attempt,
                    )
                    await session.commit()
            except Exception:
                logger.warning("Failed releasing job %s on shutdown", job_id)
        logger.info("Worker daemon stopped at %s", datetime.now(UTC).isoformat())

    if all_io_dead:
        raise RuntimeError("All daemon I/O worker tasks exited unexpectedly")
