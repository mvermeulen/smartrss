"""Dedicated regression tests for Claude review findings I-01 through I-24."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.conftest import StubSafeHttpClient, make_rss_xml
from typer.testing import CliRunner

from smartrss.cli import app as cli_app
from smartrss.db.models import AppSettings, Category, Device, Feed, Item, Job, SyncChange, SyncState
from smartrss.db.sync_log import (
    APP_SETTINGS_ENTITY_ID,
    MissingSyncEventError,
    SyncEvent,
    install_sync_guards,
    remove_sync_guards,
    sync_log,
)
from smartrss.jobs.queue import claim_next_job, enqueue_job
from smartrss.security.safe_http import (
    DomainRateLimiter,
    FetchResult,
    ResponseTooLargeError,
    SSRFBlockedError,
)
from smartrss.worker.runner import execute_claimed_job, run_worker_daemon, run_worker_once
from smartrss.worker.stages.extract_content import run_extract_content
from smartrss.worker.stages.poll_feed import run_poll_feed

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


async def test_i01_feedparser_never_opens_body_as_local_file_path(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """I-01 regression: response body equal to a local XML file path must NOT read that file."""
    feed_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(
            Feed(
                id=feed_id,
                feed_url="https://hostile.example.com/rss",
                title="Hostile Feed",
                fetch_full_text=False,
            )
        )
        await s.flush()
        await sync_log.record(s, [SyncEvent("feed", feed_id, "upsert")])
        await s.commit()

    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tf:
        tf.write(
            make_rss_xml(
                "SECRET LOCAL FILE",
                [
                    {
                        "title": "Secret Local Entry",
                        "link": "https://secret.example.com/1",
                        "guid": "s1",
                    }
                ],
            )
        )
        local_path = Path(tf.name)

    try:
        # Hostile server returns the local filesystem path as the HTTP response body
        stub_http = StubSafeHttpClient(
            lambda u, _h: FetchResult(
                200, u, None, {}, str(local_path).encode("utf-8"), "application/rss+xml"
            )
        )
        outcome = await run_poll_feed(session_factory, stub_http, feed_id=feed_id)
        assert outcome.metric_status == "failed"
        assert outcome.items_ingested == 0

        async with session_factory() as s:
            items = (
                (await s.execute(sa.select(Item).where(Item.feed_id == feed_id))).scalars().all()
            )
            assert items == []
            feed = await s.get(Feed, feed_id)
            assert feed is not None
            assert feed.title == "Hostile Feed"
            assert feed.health_state == "degraded"
    finally:
        await asyncio.to_thread(local_path.unlink, missing_ok=True)


async def test_i02_daemon_worker_slot_survives_transient_heartbeat_and_db_errors(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I-02 regression: transient DB errors in heartbeat AND completion/fail never kill a slot.

    Job 1's heartbeat fails twice (must keep retrying, not end the heartbeat task), then
    complete_job and fail_job both raise so the exception escapes execute_claimed_job into
    the daemon slot loop. The single slot (concurrency=1) must survive and run job 2.
    """
    import smartrss.worker.runner as runner_mod

    f1 = uuid.uuid4()
    f2 = uuid.uuid4()
    async with session_factory() as s:
        s.add_all(
            [
                Feed(
                    id=f1,
                    feed_url="https://s1.example.com/rss",
                    title="Slot Feed 1",
                    next_fetch_at=datetime.now(UTC) + timedelta(hours=1),
                    fetch_full_text=False,
                ),
                Feed(
                    id=f2,
                    feed_url="https://s2.example.com/rss",
                    title="Slot Feed 2",
                    next_fetch_at=datetime.now(UTC) + timedelta(hours=1),
                    fetch_full_text=False,
                ),
            ]
        )
        await s.flush()
        await sync_log.record(s, [SyncEvent("feed", f1, "upsert"), SyncEvent("feed", f2, "upsert")])
        await enqueue_job(s, job_type="poll_feed", dedupe_key=f"feed:{f1}", feed_id=f1, priority=10)
        await enqueue_job(s, job_type="poll_feed", dedupe_key=f"feed:{f2}", feed_id=f2, priority=20)
        await s.commit()

    calls = {"heartbeat": 0, "heartbeat_failed": 0, "complete_failed": 0, "fail_failed": 0}
    real_heartbeat = runner_mod.heartbeat_job
    real_complete = runner_mod.complete_job
    real_fail = runner_mod.fail_job

    async def _flaky_heartbeat(session: AsyncSession, **kwargs: Any) -> bool:
        calls["heartbeat"] += 1
        if calls["heartbeat_failed"] < 2:
            calls["heartbeat_failed"] += 1
            raise RuntimeError("transient DB disconnect (heartbeat)")
        return await real_heartbeat(session, **kwargs)

    async def _flaky_complete(session: AsyncSession, **kwargs: Any) -> bool:
        if calls["complete_failed"] < 1:
            calls["complete_failed"] += 1
            raise RuntimeError("transient DB disconnect (complete)")
        return await real_complete(session, **kwargs)

    async def _flaky_fail(session: AsyncSession, **kwargs: Any) -> tuple[bool, bool]:
        if calls["fail_failed"] < 1:
            calls["fail_failed"] += 1
            raise RuntimeError("transient DB disconnect (fail)")
        return await real_fail(session, **kwargs)

    monkeypatch.setattr(runner_mod, "heartbeat_job", _flaky_heartbeat)
    monkeypatch.setattr(runner_mod, "complete_job", _flaky_complete)
    monkeypatch.setattr(runner_mod, "fail_job", _flaky_fail)

    rss = make_rss_xml(
        "OK",
        [{"title": "Post", "link": "https://s2.example.com/1", "guid": "g-ok"}],
    )

    class _SlowThenFastHttp(StubSafeHttpClient):
        async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            if "s1.example.com" in url:
                # Long enough for >= 3 heartbeat ticks at 0.05s
                await asyncio.sleep(0.3)
            return FetchResult(200, url, None, {}, rss, "application/rss+xml")

    stub_http = _SlowThenFastHttp(lambda u, _h: FetchResult(200, u, None, {}, rss, None))
    stop_evt = asyncio.Event()
    daemon_task = asyncio.create_task(
        run_worker_daemon(
            session_factory,
            stub_http,
            concurrency=1,
            poll_interval_seconds=60.0,
            shutdown_event=stop_evt,
            shutdown_grace_seconds=2.0,
            heartbeat_interval_seconds=0.05,
            idle_poll_seconds=0.05,
        )
    )

    ingested_f2 = False
    for _ in range(100):
        async with session_factory() as s:
            cnt = (
                await s.execute(
                    sa.select(sa.func.count()).select_from(Item).where(Item.feed_id == f2)
                )
            ).scalar_one()
            if cnt == 1:
                ingested_f2 = True
                break
        await asyncio.sleep(0.05)

    stop_evt.set()
    await daemon_task

    # Heartbeat failed twice but kept renewing afterwards (did not die on first error)
    assert calls["heartbeat_failed"] == 2
    assert calls["heartbeat"] >= 3
    # The completion AND failure paths both raised, escaping into the daemon slot loop
    assert calls["complete_failed"] == 1
    assert calls["fail_failed"] == 1
    # ...and the single slot survived to execute job 2
    assert ingested_f2 is True
    assert not daemon_task.cancelled()
    assert daemon_task.exception() is None


async def test_i03_run_once_does_not_spin_on_foreign_unexpired_running_lease(
    db_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """I-03 regression: unexpired running job from a dead previous run is deferred, not spun on."""
    async with session_factory() as s:
        s.add(
            Job(
                job_type="poll_feed",
                dedupe_key="feed:foreign-orphan",
                status="running",
                locked_by="run-once:dead-container:999:0",
                locked_at=datetime.now(UTC),
                started_at=datetime.now(UTC),
                lease_expires_at=datetime.now(UTC) + timedelta(seconds=90),
                attempts=1,
            )
        )
        await s.commit()

    stub_http = StubSafeHttpClient(lambda u, _h: FetchResult(200, u, None, {}, b"", None))
    t0 = time.monotonic()
    summary = await run_worker_once(
        db_engine,
        session_factory,
        stub_http,
        stage="poll",
        max_duration_seconds=30.0,
        install_signals=False,
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0, f"run_worker_once spun for {elapsed:.2f}s on foreign running job"
    assert summary.status == "completed"
    assert summary.exit_code == 0
    assert summary.jobs_deferred == 1


async def test_i04_concurrent_patch_feed_url_during_poll_is_not_overwritten(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """I-04 regression: PATCH feed_url during poll discards stale URL results and repolls."""
    client: httpx.AsyncClient = api_env["client"]
    r_sub = await client.post("/api/v1/feeds", json={"feed_url": "https://old.example.com/rss"})
    feed_id = uuid.UUID(r_sub.json()["id"])

    in_http = asyncio.Event()
    release_http = asyncio.Event()

    old_rss = make_rss_xml(
        "Old Redirected Feed",
        [{"title": "Old Item", "link": "https://old.example.com/1", "guid": "old-1"}],
    )

    class _BlockedOldFetch(StubSafeHttpClient):
        async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            in_http.set()
            await release_http.wait()
            return FetchResult(
                200,
                "https://old-redirected.example.com/rss",
                "https://old-redirected.example.com/rss",
                {"etag": '"old-etag"'},
                old_rss,
                "application/rss+xml",
            )

    # Claim the initial poll_feed job and run it via execute_claimed_job
    async with session_factory() as s:
        job = await claim_next_job(s, worker_id="w-i04", job_types=["poll_feed"])
        assert job is not None
        await s.commit()

    exec_task = asyncio.create_task(
        execute_claimed_job(
            session_factory,
            _BlockedOldFetch(lambda u, _h: FetchResult(200, u, None, {}, b"", None)),
            DomainRateLimiter(min_interval_seconds=0.0),
            job=job,
            worker_id="w-i04",
        )
    )
    await in_http.wait()

    # While HTTP for old.example.com is in flight, user edits feed_url to new.example.com
    r_patch = await client.patch(
        f"/api/v1/feeds/{feed_id}",
        json={"feed_url": "https://new.example.com/rss"},
    )
    assert r_patch.status_code == 200

    release_http.set()
    await exec_task

    async with session_factory() as s:
        feed = await s.get(Feed, feed_id)
        assert feed is not None
        assert feed.feed_url == "https://new.example.com/rss"
        assert feed.initial_backfill_done is False
        assert feed.etag is None

        # Zero items from old URL inserted
        items = (await s.execute(sa.select(Item).where(Item.feed_id == feed_id))).scalars().all()
        assert items == []

        # A fresh queued priority-10 poll_feed job exists for the new URL
        queued = (
            await s.execute(
                sa.select(Job).where(
                    Job.job_type == "poll_feed",
                    Job.feed_id == feed_id,
                    Job.status == "queued",
                )
            )
        ).scalar_one_or_none()
        assert queued is not None
        assert queued.priority == 10


async def test_i04_unexpected_poll_failure_after_patch_does_not_degrade_new_url(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """I-04 regression: an unexpected poll exception after a mid-poll PATCH feed_url must not
    degrade the newly edited feed's health, and must queue a priority-10 repoll."""
    client: httpx.AsyncClient = api_env["client"]
    r_sub = await client.post(
        "/api/v1/feeds", json={"feed_url": "https://crash-old.example.com/rss"}
    )
    feed_id = uuid.UUID(r_sub.json()["id"])

    in_http = asyncio.Event()
    release_http = asyncio.Event()

    class _CrashingFetch(StubSafeHttpClient):
        async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            in_http.set()
            await release_http.wait()
            # Not a SafeHttpError/httpx/OSError: escapes run_poll_feed as unexpected
            raise RuntimeError("unexpected parser/driver crash")

    async with session_factory() as s:
        job = await claim_next_job(s, worker_id="w-i04b", job_types=["poll_feed"])
        assert job is not None
        await s.commit()

    exec_task = asyncio.create_task(
        execute_claimed_job(
            session_factory,
            _CrashingFetch(lambda u, _h: FetchResult(200, u, None, {}, b"", None)),
            DomainRateLimiter(min_interval_seconds=0.0),
            job=job,
            worker_id="w-i04b",
        )
    )
    await in_http.wait()
    r_patch = await client.patch(
        f"/api/v1/feeds/{feed_id}",
        json={"feed_url": "https://crash-new.example.com/rss"},
    )
    assert r_patch.status_code == 200
    release_http.set()
    await exec_task

    async with session_factory() as s:
        feed = await s.get(Feed, feed_id)
        assert feed is not None
        assert feed.feed_url == "https://crash-new.example.com/rss"
        assert feed.health_state == "healthy"
        assert feed.consecutive_errors == 0
        assert feed.last_error is None

        queued = (
            await s.execute(
                sa.select(Job).where(
                    Job.job_type == "poll_feed",
                    Job.feed_id == feed_id,
                    Job.status == "queued",
                )
            )
        ).scalar_one_or_none()
        assert queued is not None
        assert queued.priority == 10
        assert queued.run_after <= datetime.now(UTC)


async def test_i04_unexpected_poll_failure_without_patch_still_degrades(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Counterpart: with no concurrent PATCH, an unexpected crash still records the error."""
    feed_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(Feed(id=feed_id, feed_url="https://crash.example.com/rss", title="Crash"))
        await s.flush()
        await sync_log.record(s, [SyncEvent("feed", feed_id, "upsert")])
        await enqueue_job(
            s, job_type="poll_feed", dedupe_key=f"feed:{feed_id}", feed_id=feed_id, priority=10
        )
        await s.commit()
        job = await claim_next_job(s, worker_id="w-i04c", job_types=["poll_feed"])
        assert job is not None
        await s.commit()

    class _Crash(StubSafeHttpClient):
        async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            raise RuntimeError("boom")

    await execute_claimed_job(
        session_factory,
        _Crash(lambda u, _h: FetchResult(200, u, None, {}, b"", None)),
        DomainRateLimiter(min_interval_seconds=0.0),
        job=job,
        worker_id="w-i04c",
    )
    async with session_factory() as s:
        feed = await s.get(Feed, feed_id)
        assert feed is not None
        assert feed.health_state == "degraded"
        assert feed.consecutive_errors == 1
        assert feed.last_error == "RuntimeError: boom"


async def test_i05_large_feed_3000_entries_chunked_insert(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """I-05 regression: 3,000 entries in one poll insert in chunks without hitting 65,535 params."""
    feed_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(
            Feed(
                id=feed_id,
                feed_url="https://archive.example.com/rss",
                title="Big Archive",
                fetch_full_text=False,
            )
        )
        await s.flush()
        await sync_log.record(s, [SyncEvent("feed", feed_id, "upsert")])
        await s.commit()

    entries = [
        {
            "title": f"Episode {i}",
            "link": f"https://archive.example.com/ep/{i}",
            "guid": f"ep-{i}",
            "summary": f"Notes {i}",
        }
        for i in range(3000)
    ]
    rss_bytes = make_rss_xml("Big Archive", entries)
    stub = StubSafeHttpClient(
        lambda u, _h: FetchResult(200, u, None, {}, rss_bytes, "application/rss+xml")
    )
    outcome = await run_poll_feed(session_factory, stub, feed_id=feed_id)
    assert outcome.metric_status == "ok"
    assert outcome.items_ingested == 3000

    async with session_factory() as s:
        unread = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(Item)
                .where(Item.feed_id == feed_id, Item.state == "unread")
            )
        ).scalar_one()
        expired = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(Item)
                .where(Item.feed_id == feed_id, Item.state == "expired")
            )
        ).scalar_one()
        assert unread == 10
        assert expired == 2990


async def test_i06_relative_links_and_http_charset_in_poll_feed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """I-06 regression: Content-Location resolves relative links; Content-Type charset honored."""
    feed_id = uuid.uuid4()
    latin_feed_id = uuid.uuid4()
    async with session_factory() as s:
        s.add_all(
            [
                Feed(
                    id=feed_id,
                    feed_url="https://eng.example.com/feed.xml",
                    title="Eng",
                    fetch_full_text=False,
                ),
                Feed(
                    id=latin_feed_id,
                    feed_url="https://latin.example.com/feed.xml",
                    title="Latin",
                    fetch_full_text=False,
                ),
            ]
        )
        await s.flush()
        await sync_log.record(
            s, [SyncEvent("feed", feed_id, "upsert"), SyncEvent("feed", latin_feed_id, "upsert")]
        )
        await s.commit()

    fixture_rss = (FIXTURES_DIR / "rss_sample.xml").read_bytes()
    stub1 = StubSafeHttpClient(
        lambda u, _h: FetchResult(
            200, u, None, {}, fixture_rss, "application/rss+xml; charset=utf-8"
        )
    )
    out1 = await run_poll_feed(session_factory, stub1, feed_id=feed_id)
    assert out1.metric_status == "ok"
    assert out1.items_ingested == 2

    async with session_factory() as s:
        rel_item = (
            await s.execute(
                sa.select(Item).where(Item.feed_id == feed_id, Item.guid == "urn:eng:post-2")
            )
        ).scalar_one()
        assert rel_item.canonical_url == "https://eng.example.com/posts/relative-42"

    # ISO-8859-1 feed with charset only in HTTP Content-Type header (no XML encoding attr)
    latin_rss = (
        "<rss version='2.0'><channel><title>Café Tech</title>"
        "<item><title>Résumé</title><link>https://latin.example.com/1</link><guid>l1</guid></item>"
        "</channel></rss>"
    ).encode("iso-8859-1")
    stub2 = StubSafeHttpClient(
        lambda u, _h: FetchResult(
            200, u, None, {}, latin_rss, "application/rss+xml; charset=iso-8859-1"
        )
    )
    out2 = await run_poll_feed(session_factory, stub2, feed_id=latin_feed_id)
    assert out2.metric_status == "ok"
    async with session_factory() as s:
        item = (await s.execute(sa.select(Item).where(Item.feed_id == latin_feed_id))).scalar_one()
        assert item.title == "Résumé"

    # Feed where ALL entries have invalid links degrades health cleanly instead of silent healthy
    bad_links_rss = make_rss_xml(
        "All Bad Links",
        [{"title": "No Scheme", "link": "javascript:void(0)", "guid": "b1"}],
    )
    stub_bad = StubSafeHttpClient(
        lambda u, _h: FetchResult(200, u, None, {}, bad_links_rss, "application/rss+xml")
    )
    out_bad = await run_poll_feed(session_factory, stub_bad, feed_id=latin_feed_id)
    assert out_bad.metric_status == "failed"

    # 304 and 410 racing with concurrent PATCH feed_url both discard stale result & request repoll
    async def _race_status(status_code: int) -> None:
        in_io = asyncio.Event()
        release_io = asyncio.Event()

        class _RaceHttp(StubSafeHttpClient):
            async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
                in_io.set()
                await release_io.wait()
                return FetchResult(status_code, url, None, {}, b"", None)

        task = asyncio.create_task(
            run_poll_feed(
                session_factory,
                _RaceHttp(lambda u, _h: FetchResult(status_code, u, None, {}, b"", None)),
                feed_id=feed_id,
            )
        )
        await in_io.wait()
        async with session_factory() as s:
            f = await s.get(Feed, feed_id)
            assert f is not None
            f.feed_url = f"https://raced-{status_code}.example.com/rss"
            await s.flush()
            await sync_log.record(s, [SyncEvent("feed", feed_id, "upsert")])
            await s.commit()
        release_io.set()
        res = await task
        assert res.repoll_needed is True
        assert res.metric_status == "skipped"

    await _race_status(304)
    await _race_status(410)


async def test_i07_permanent_extraction_failures_and_meta_charset(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """I-07: fetch_full_text=False, SSRF, 404, non-HTML, too-large, and meta-charset extraction."""
    f_no_ft = uuid.uuid4()
    f_ft = uuid.uuid4()
    now = datetime.now(UTC)

    i_no_ft = uuid.uuid4()
    i_ssrf = uuid.uuid4()
    i_pdf = uuid.uuid4()
    i_large = uuid.uuid4()
    i_latin = uuid.uuid4()

    async with session_factory() as s:
        s.add_all(
            [
                Feed(
                    id=f_no_ft,
                    feed_url="https://noft.example.com/rss",
                    title="NoFT",
                    fetch_full_text=False,
                ),
                Feed(
                    id=f_ft,
                    feed_url="https://ft.example.com/rss",
                    title="FT",
                    fetch_full_text=True,
                ),
            ]
        )
        await s.flush()
        specs = [
            (i_no_ft, f_no_ft, "https://noft.example.com/1"),
            (i_ssrf, f_ft, "https://ft.example.com/ssrf"),
            (i_pdf, f_ft, "https://ft.example.com/doc.pdf"),
            (i_large, f_ft, "https://ft.example.com/huge"),
            (i_latin, f_ft, "https://ft.example.com/latin"),
        ]
        events = [SyncEvent("feed", f_no_ft, "upsert"), SyncEvent("feed", f_ft, "upsert")]
        for iid, fid, url in specs:
            s.add(
                Item(
                    id=iid,
                    feed_id=fid,
                    guid=str(iid),
                    raw_url=url,
                    canonical_url=url,
                    title="Test",
                    fetched_at=now,
                    read_state_updated_at=now,
                    starred_updated_at=now,
                )
            )
            events.append(SyncEvent("item", iid, "upsert"))
        await s.flush()
        await sync_log.record(s, events)
        await s.commit()

    latin1_bytes = (
        (FIXTURES_DIR / "article_latin1.html")
        .read_text(encoding="utf-8")
        .replace("&#233;", "é")
        .replace("&#239;", "ï")
        .encode("latin-1")
    )

    def _handler(url: str, _h: dict[str, str]) -> FetchResult | Exception:
        if url.endswith("/ssrf"):
            return SSRFBlockedError("private")
        if url.endswith("/huge"):
            return ResponseTooLargeError("too large")
        if url.endswith("/doc.pdf"):
            return FetchResult(200, url, None, {}, b"%PDF-1.7", "application/pdf")
        return FetchResult(200, url, None, {}, latin1_bytes, "text/html")

    stub = StubSafeHttpClient(_handler)
    limiter = DomainRateLimiter(min_interval_seconds=0.0)

    for iid in (i_no_ft, i_ssrf, i_pdf, i_large):
        res = await run_extract_content(session_factory, stub, limiter, item_id=iid)
        assert res.extraction_status == "fallback_rss"

    res_latin = await run_extract_content(session_factory, stub, limiter, item_id=i_latin)
    assert res_latin.extraction_status == "ok"


async def test_i10_i11_i13_i14_api_minor_regressions(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regressions for I-10 (OPML no live DNS), I-11 (site_url sanitize), I-13, I-14."""
    client: httpx.AsyncClient = api_env["client"]

    # I-10 & I-11: OPML fixture has javascript: htmlUrl and literal private IP
    opml_xml = (FIXTURES_DIR / "opml_sample.xml").read_text(encoding="utf-8")
    r_imp = await client.post("/api/v1/feeds/import-opml", json={"opml_xml": opml_xml})
    assert r_imp.status_code == 200
    assert r_imp.json() == {
        "imported": 2,
        "reactivated": 0,
        "skipped_duplicates": 0,
        "rejected_invalid": 1,
    }

    feeds = {f["feed_url"]: f for f in (await client.get("/api/v1/feeds")).json()}
    assert feeds["https://alpha.example.com/rss"]["site_url"] == "https://alpha.example.com"
    assert feeds["https://beta.example.com/rss"]["site_url"] is None  # javascript: stripped!

    # I-13: Reactivating unsubscribed feed without fetch_full_text preserves user's False setting
    beta_id = feeds["https://beta.example.com/rss"]["id"]
    await client.patch(f"/api/v1/feeds/{beta_id}", json={"fetch_full_text": False})
    await client.patch(f"/api/v1/feeds/{beta_id}", json={"status": "unsubscribed"})

    r_resub = await client.post(
        "/api/v1/feeds",
        json={"feed_url": "https://beta.example.com/rss"},
    )
    assert r_resub.status_code == 201
    assert r_resub.json()["fetch_full_text"] is False

    # I-14: Manual poll while job is running returns queued=False
    async with session_factory() as s:
        claimed = await claim_next_job(
            s,
            worker_id="w-busy",
            job_types=["poll_feed"],
            feed_id=uuid.UUID(beta_id),
        )
        assert claimed is not None
        await s.commit()

    r_poll_busy = await client.post(f"/api/v1/feeds/{beta_id}/poll")
    assert r_poll_busy.status_code == 200
    assert r_poll_busy.json()["queued"] is False
    assert r_poll_busy.json()["job_id"] is None


async def test_i07_i18_cli_settings_pair_devices_and_rotate_epoch_env(
    test_db_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """I-07 & I-18: smartrss settings get/set, pair/devices, and rotate-epoch-env."""
    runner = CliRunner()
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    from smartrss.config import get_settings

    get_settings.cache_clear()
    try:
        r_set = await asyncio.to_thread(
            runner.invoke, cli_app, ["settings", "set", "backfill_cap", "15"]
        )
        assert r_set.exit_code == 0, r_set.output

        r_get = await asyncio.to_thread(runner.invoke, cli_app, ["settings", "get"])
        assert r_get.exit_code == 0, r_get.output
        assert json.loads(r_get.output)["backfill_cap"] == 15

        async with session_factory() as s:
            ev = (
                await s.execute(
                    sa.select(SyncChange).where(
                        SyncChange.entity_type == "app_settings",
                        SyncChange.entity_id == APP_SETTINGS_ENTITY_ID,
                    )
                )
            ).scalar_one_or_none()
            assert ev is not None

        r_pair = await asyncio.to_thread(
            runner.invoke, cli_app, ["pair", "create", "--name", "CLIBox", "--platform", "ubuntu"]
        )
        assert r_pair.exit_code == 0, r_pair.output
        assert "Pairing code:" in r_pair.output

        r_list = await asyncio.to_thread(runner.invoke, cli_app, ["devices", "list"])
        assert r_list.exit_code == 0

        # Seed a device and revoke via CLI
        dev_id = uuid.uuid4()
        async with session_factory() as s:
            s.add(Device(id=dev_id, name="RevokeMe", platform="web", token_hash="h" * 64))
            await s.commit()

        r_rev = await asyncio.to_thread(runner.invoke, cli_app, ["devices", "revoke", str(dev_id)])
        assert r_rev.exit_code == 0, r_rev.output

        # I-18: rotate-epoch via --database-url-env avoids password in CLI argv
        async with session_factory() as s:
            before = (await s.get(SyncState, 1)).instance_epoch  # type: ignore[union-attr]

        monkeypatch.setenv("TEST_ROTATE_TARGET_URL", test_db_url)
        r_rot = await asyncio.to_thread(
            runner.invoke,
            cli_app,
            ["db", "rotate-epoch", "--database-url-env", "TEST_ROTATE_TARGET_URL"],
        )
        assert r_rot.exit_code == 0, r_rot.output

        async with session_factory() as s:
            after = (await s.get(SyncState, 1)).instance_epoch  # type: ignore[union-attr]
            assert after != before
    finally:
        get_settings.cache_clear()


async def test_i08_synclog_empty_record_and_guard_install_remove(
    db_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Exercise SyncLogWriter edge cases and guard install/remove cleanly."""
    async with session_factory() as s:
        assert await sync_log.record(s, []) == []

        # ORM delete of a synced entity without sync_log.record raises MissingSyncEventError
        cid = uuid.uuid4()
        cat = Category(id=cid, name="TempCat")
        s.add(cat)
        await s.flush()
        await sync_log.record(s, [SyncEvent("category", cid, "upsert")])
        await s.commit()

    async with session_factory() as s:
        cat_row = await s.get(Category, cid)
        assert cat_row is not None
        await s.delete(cat_row)
        with pytest.raises(MissingSyncEventError):
            await s.commit()
        await s.rollback()

    # remove_sync_guards + re-install round-trip
    remove_sync_guards(db_engine, session_factory)
    try:
        async with session_factory() as s:
            settings_row = await s.get(AppSettings, 1)
            assert settings_row is not None
            settings_row.backfill_cap = 11
            await s.commit()
    finally:
        install_sync_guards(db_engine, session_factory)
