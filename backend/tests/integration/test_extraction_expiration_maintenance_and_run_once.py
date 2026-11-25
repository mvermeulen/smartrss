"""Integration tests for extraction, expiration, retention cleanup, run-once, metrics & CLI."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.conftest import StubSafeHttpClient, make_rss_xml
from typer.testing import CliRunner

from smartrss.cli import EX_CONFIG
from smartrss.cli import app as cli_app
from smartrss.db.models import (
    AppSettings,
    Feed,
    Item,
    Job,
    PipelineRun,
    StageMetric,
    SyncChange,
    SyncState,
)
from smartrss.db.sync_log import SyncEvent, sync_log
from smartrss.jobs.locks import LOCK_RUN_ONCE, advisory_session_lock
from smartrss.jobs.queue import claim_next_job, enqueue_job
from smartrss.security.safe_http import DomainRateLimiter, FetchResult, SSRFBlockedError
from smartrss.worker.runner import execute_claimed_job, run_worker_once
from smartrss.worker.stages.expire_items import run_expire_items
from smartrss.worker.stages.extract_content import run_extract_content
from smartrss.worker.stages.maintenance import run_retention_cleanup

ARTICLE_HTML = b"""<!doctype html>
<html><head><title>Deep Dive</title></head><body><main><article>
<h1>Deep Dive on Distributed Sync</h1>
<p>SmartRSS uses a commit-ordered append-only sync_changes log backed by a single-row
sync_state watermark locked via SELECT FOR UPDATE right before transaction commit.</p>
<p>This guarantees gap-free incremental pulls across laptops and phones without losing
updates when concurrent database transactions commit out of start order.</p>
</article></main></body></html>"""


async def test_extraction_ok_fallback_and_exhausted_retries_failed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    feed_id = uuid.uuid4()
    item_ok = uuid.uuid4()
    item_403 = uuid.uuid4()
    item_503 = uuid.uuid4()
    now = datetime.now(UTC)

    async with session_factory() as s:
        s.add(Feed(id=feed_id, feed_url="https://ext.example.com/rss", title="Ext Feed"))
        await s.flush()
        for iid, slug in ((item_ok, "ok"), (item_403, "403"), (item_503, "503")):
            s.add(
                Item(
                    id=iid,
                    feed_id=feed_id,
                    guid=f"g-{slug}",
                    raw_url=f"https://ext.example.com/{slug}",
                    canonical_url=f"https://ext.example.com/{slug}",
                    title=f"Title {slug}",
                    fetched_at=now,
                    raw_summary="Original RSS summary",
                    entry_hash="orig-entry-hash",
                    content_hash="orig-entry-hash",
                    read_state_updated_at=now,
                    starred_updated_at=now,
                )
            )
        await s.flush()
        await sync_log.record(
            s,
            [
                SyncEvent("feed", feed_id, "upsert"),
                SyncEvent("item", item_ok, "upsert"),
                SyncEvent("item", item_403, "upsert"),
                SyncEvent("item", item_503, "upsert"),
            ],
        )
        await enqueue_job(
            s,
            job_type="extract_content",
            dedupe_key=f"item:{item_503}",
            feed_id=feed_id,
            item_id=item_503,
            max_attempts=1,
        )
        await s.commit()

    def _handler(url: str, _hdrs: dict[str, str]) -> FetchResult | Exception:
        if url.endswith("/ok"):
            return FetchResult(200, url, None, {}, ARTICLE_HTML, "text/html; charset=utf-8")
        if url.endswith("/403"):
            return FetchResult(403, url, None, {}, b"Forbidden", "text/html")
        if url.endswith("/ssrf"):
            return SSRFBlockedError("blocked")
        return FetchResult(503, url, None, {}, b"Unavailable", "text/html")

    stub_http = StubSafeHttpClient(_handler)
    limiter = DomainRateLimiter(min_interval_seconds=0.0)

    # 1. Clean HTML -> extraction_status='ok', content_hash updated, entry_hash preserved
    r_ok = await run_extract_content(session_factory, stub_http, limiter, item_id=item_ok)
    assert r_ok.extraction_status == "ok"
    async with session_factory() as s:
        row = await s.get(Item, item_ok)
        assert row is not None
        assert row.extraction_status == "ok"
        assert row.extracted_text is not None and "sync_changes" in row.extracted_text
        assert row.entry_hash == "orig-entry-hash"
        assert row.content_hash != "orig-entry-hash"

    # 2. Permanent 403 -> fallback_rss immediately (no retry)
    r_403 = await run_extract_content(session_factory, stub_http, limiter, item_id=item_403)
    assert r_403.extraction_status == "fallback_rss"

    # 3. Exhausted transient 503 via execute_claimed_job -> extraction_status='failed' + item upsert
    async with session_factory() as s:
        job = await claim_next_job(s, worker_id="w-ext", job_types=["extract_content"])
        assert job is not None
        await s.commit()

    await execute_claimed_job(session_factory, stub_http, limiter, job=job, worker_id="w-ext")

    async with session_factory() as s:
        row_503 = await s.get(Item, item_503)
        job_503 = await s.get(Job, job.id)
        assert row_503 is not None and row_503.extraction_status == "failed"
        assert row_503.raw_summary == "Original RSS summary"
        assert job_503 is not None and job_503.status == "failed"


async def test_expire_items_sweeper_invariants(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=40)
    recent = now - timedelta(days=2)

    f_default = uuid.uuid4()
    f_never = uuid.uuid4()
    i_expire = uuid.uuid4()
    i_starred = uuid.uuid4()
    i_unexpired = uuid.uuid4()
    i_never_feed = uuid.uuid4()

    async with session_factory() as s:
        s.add(Feed(id=f_default, feed_url="https://exp.example.com/rss", title="Default TTL"))
        s.add(
            Feed(
                id=f_never,
                feed_url="https://never.example.com/rss",
                title="Never Expire",
                expiration_days_override=0,
            )
        )
        await s.flush()
        s.add_all(
            [
                Item(
                    id=i_expire,
                    feed_id=f_default,
                    guid="exp",
                    raw_url="https://exp.example.com/1",
                    canonical_url="https://exp.example.com/1",
                    title="Should expire",
                    fetched_at=old,
                    state="unread",
                    is_starred=False,
                    read_state_updated_at=old,
                    starred_updated_at=old,
                ),
                Item(
                    id=i_starred,
                    feed_id=f_default,
                    guid="star",
                    raw_url="https://exp.example.com/2",
                    canonical_url="https://exp.example.com/2",
                    title="Starred immune",
                    fetched_at=old,
                    state="unread",
                    is_starred=True,
                    read_state_updated_at=old,
                    starred_updated_at=old,
                ),
                Item(
                    id=i_unexpired,
                    feed_id=f_default,
                    guid="unexp",
                    raw_url="https://exp.example.com/3",
                    canonical_url="https://exp.example.com/3",
                    title="Grace reset",
                    fetched_at=old,
                    unexpired_at=recent,
                    state="unread",
                    is_starred=False,
                    read_state_updated_at=recent,
                    starred_updated_at=old,
                ),
                Item(
                    id=i_never_feed,
                    feed_id=f_never,
                    guid="never",
                    raw_url="https://never.example.com/1",
                    canonical_url="https://never.example.com/1",
                    title="Feed override 0",
                    fetched_at=old,
                    state="unread",
                    is_starred=False,
                    read_state_updated_at=old,
                    starred_updated_at=old,
                ),
            ]
        )
        await s.flush()
        await sync_log.record(
            s,
            [
                SyncEvent("feed", f_default, "upsert"),
                SyncEvent("feed", f_never, "upsert"),
                SyncEvent("item", i_expire, "upsert"),
                SyncEvent("item", i_starred, "upsert"),
                SyncEvent("item", i_unexpired, "upsert"),
                SyncEvent("item", i_never_feed, "upsert"),
            ],
        )
        await s.commit()

    expired_count = await run_expire_items(session_factory, batch_size=2)
    assert expired_count == 1

    async with session_factory() as s:
        assert (await s.get(Item, i_expire)).state == "expired"  # type: ignore[union-attr]
        assert (await s.get(Item, i_starred)).state == "unread"  # type: ignore[union-attr]
        assert (await s.get(Item, i_unexpired)).state == "unread"  # type: ignore[union-attr]
        assert (await s.get(Item, i_never_feed)).state == "unread"  # type: ignore[union-attr]


async def test_retention_cleanup_fk_safe_and_zero_sync_events(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    old_200d = datetime.now(UTC) - timedelta(days=200)
    old_100d = datetime.now(UTC) - timedelta(days=100)
    old_40d = datetime.now(UTC) - timedelta(days=40)

    old_run_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(
            PipelineRun(
                id=old_run_id,
                trigger="cron_cli",
                stages=["poll_feed"],
                status="completed",
                started_at=old_200d,
                finished_at=old_200d,
                duration_ms=100,
            )
        )
        await s.flush()
        # Retained dead-letter job referencing the 200d old pipeline_run -> run_id must become NULL!
        dead_job = Job(
            job_type="poll_feed",
            dedupe_key="feed:old-dead",
            run_id=old_run_id,
            status="failed",
            finished_at=old_200d,
            last_error="retained",
        )
        done_job = Job(
            job_type="poll_feed",
            dedupe_key="feed:old-done",
            status="done",
            finished_at=old_40d,
        )
        s.add_all([dead_job, done_job])
        s.add(
            StageMetric(
                run_id=old_run_id,
                job_type="poll_feed",
                status="ok",
                queue_wait_ms=1,
                duration_ms=10,
                created_at=old_200d,
            )
        )
        # Old sync_changes row
        await sync_log.record(s, [SyncEvent("item", uuid.uuid4(), "upsert")])
        await s.execute(sa.update(SyncChange).values(changed_at=old_100d))
        await s.commit()
        dead_job_id = dead_job.id

    async with session_factory() as s:
        res = await run_retention_cleanup(s)
        await s.commit()

    assert res.jobs_deleted == 1
    assert res.sync_changes_deleted == 1
    assert res.stage_metrics_deleted == 1
    assert res.pipeline_runs_deleted == 1

    async with session_factory() as s:
        retained_dead = await s.get(Job, dead_job_id)
        assert retained_dead is not None
        assert retained_dead.run_id is None  # ON DELETE SET NULL preserved dead-letter job
        settings = await s.get(AppSettings, 1)
        assert settings is not None and settings.last_retention_cleanup_at is not None
        # Zero new sync_changes emitted for internal last_retention_cleanup_at update
        sc_count = (
            await s.execute(sa.select(sa.func.count()).select_from(SyncChange))
        ).scalar_one()
        assert sc_count == 0


async def test_worker_run_once_overlap_deferred_and_metrics_api(
    api_env: dict[str, Any],
    db_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client: httpx.AsyncClient = api_env["client"]

    # Create a due feed
    r_feed = await client.post("/api/v1/feeds", json={"feed_url": "https://batch.example.com/rss"})
    feed_id = uuid.UUID(r_feed.json()["id"])

    # Also add a future-deferred job so run-once doesn't hang and reports jobs_deferred=1
    async with session_factory() as s:
        await enqueue_job(
            s,
            job_type="poll_feed",
            dedupe_key="feed:deferred-example",
            run_after=datetime.now(UTC) + timedelta(minutes=15),
        )
        await s.commit()

    rss = make_rss_xml(
        "Batch Feed",
        [
            {
                "title": "Batch Article",
                "link": "https://batch.example.com/a1",
                "guid": "b1",
                "summary": "Short",
            }
        ],
    )

    def _handler(url: str, _hdrs: dict[str, str]) -> FetchResult:
        if url.endswith("/rss"):
            return FetchResult(200, url, None, {}, rss, "application/rss+xml")
        return FetchResult(200, url, None, {}, ARTICLE_HTML, "text/html")

    stub_http = StubSafeHttpClient(_handler)

    # 1. Overlapping invocation while LOCK_RUN_ONCE is held -> skipped_locked (exit 0)
    async with advisory_session_lock(db_engine, LOCK_RUN_ONCE) as held:
        assert held is True
        overlap = await run_worker_once(db_engine, session_factory, stub_http, stage="all")
        assert overlap.status == "skipped_locked"
        assert overlap.exit_code == 0

    # 2. Real run-once drains poll + extract + expire + due retention_cleanup and reports deferred=1
    summary = await run_worker_once(
        db_engine,
        session_factory,
        stub_http,
        stage="all",
        rate_limiter=DomainRateLimiter(min_interval_seconds=0.0),
    )
    assert summary.status == "completed"
    assert summary.exit_code == 0
    assert summary.items_polled == 1
    assert summary.items_extracted == 1
    assert summary.jobs_deferred == 1

    # Verify /api/v1/system/health & /api/v1/system/metrics (exact SQL p50/p95)
    r_sys = await client.get("/api/v1/system/health")
    assert r_sys.status_code == 200
    assert r_sys.json()["db"] == "ok"
    assert len(r_sys.json()["recent_runs"]) >= 1

    # Insert deterministic stage_metrics durations [10, 20, 30, 40, 100] to verify exact SQL p50/p95
    async with session_factory() as s:
        for dur in (10, 20, 30, 40, 100):
            s.add(
                StageMetric(
                    job_type="synthetic_stage",
                    feed_id=feed_id,
                    status="ok",
                    queue_wait_ms=5,
                    duration_ms=dur,
                )
            )
        await s.commit()

    r_metrics = await client.get("/api/v1/system/metrics?window=24h")
    assert r_metrics.status_code == 200
    stages = {st["job_type"]: st for st in r_metrics.json()["stages"]}
    syn = stages["synthetic_stage"]
    assert syn["p50_duration_ms"] == 30.0
    assert (
        syn["p95_duration_ms"] == 88.0
    )  # linear interpolation at 0.95 * 4 = 3.8 -> 40 + 0.8*60 = 88
    assert syn["max_duration_ms"] == 100
    assert syn["avg_duration_ms"] == 40.0
    assert any(pf["feed_id"] == str(feed_id) for pf in r_metrics.json()["per_feed"])

    # Verify items endpoint returns the extracted item
    r_items = await client.get(f"/api/v1/items?feed_id={feed_id}")
    assert r_items.status_code == 200
    assert r_items.json()["total"] == 1
    item_id = r_items.json()["items"][0]["id"]
    r_item = await client.get(f"/api/v1/items/{item_id}")
    assert r_item.status_code == 200
    assert r_item.json()["extraction_status"] == "ok"


async def test_cli_worker_cron_guard_and_rotate_epoch(
    test_db_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    runner = CliRunner()

    # 1. smartrss worker run refuses to start when WORKER_MODE=cron (exit 78)
    monkeypatch.setenv("WORKER_MODE", "cron")
    from smartrss.config import get_settings

    get_settings.cache_clear()
    try:
        res = runner.invoke(cli_app, ["worker", "run"])
        assert res.exit_code == EX_CONFIG
    finally:
        monkeypatch.delenv("WORKER_MODE", raising=False)
        get_settings.cache_clear()

    # 2. smartrss db rotate-epoch rotates sync_state.instance_epoch on explicit target DB
    async with session_factory() as s:
        before_epoch = (await s.get(SyncState, 1)).instance_epoch  # type: ignore[union-attr]

    # Run CLI command in a thread so asyncio.run() doesn't clash with pytest's event loop
    res_rot = await asyncio.to_thread(
        runner.invoke,
        cli_app,
        ["db", "rotate-epoch", "--database-url", test_db_url],
    )
    assert res_rot.exit_code == 0, res_rot.output

    async with session_factory() as s:
        after_epoch = (await s.get(SyncState, 1)).instance_epoch  # type: ignore[union-attr]
        assert after_epoch != before_epoch


async def test_worker_daemon_runs_scheduler_processes_job_and_shuts_down(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from smartrss.worker.runner import run_worker_daemon

    feed_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(
            Feed(
                id=feed_id,
                feed_url="https://daemon.example.com/rss",
                title="Daemon Feed",
                next_fetch_at=datetime.now(UTC) - timedelta(minutes=1),
                fetch_full_text=False,
            )
        )
        await s.flush()
        await sync_log.record(s, [SyncEvent("feed", feed_id, "upsert")])
        await s.commit()

    rss = make_rss_xml(
        "Daemon Feed",
        [{"title": "Daemon Post", "link": "https://daemon.example.com/1", "guid": "d1"}],
    )
    stub_http = StubSafeHttpClient(
        lambda u, _h: FetchResult(200, u, None, {}, rss, "application/rss+xml")
    )
    stop_evt = asyncio.Event()
    daemon_task = asyncio.create_task(
        run_worker_daemon(
            session_factory,
            stub_http,
            concurrency=2,
            poll_interval_seconds=0.1,
            shutdown_event=stop_evt,
            shutdown_grace_seconds=2.0,
        )
    )

    # Wait until the daemon polls the feed and ingests the item
    for _ in range(40):
        async with session_factory() as s:
            cnt = (
                await s.execute(
                    sa.select(sa.func.count()).select_from(Item).where(Item.feed_id == feed_id)
                )
            ).scalar_one()
            if cnt == 1:
                break
        await asyncio.sleep(0.05)

    stop_evt.set()
    await daemon_task

    async with session_factory() as s:
        cnt = (
            await s.execute(
                sa.select(sa.func.count()).select_from(Item).where(Item.feed_id == feed_id)
            )
        ).scalar_one()
        assert cnt == 1
