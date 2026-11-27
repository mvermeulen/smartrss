"""Integration regression tests for 1a–1c user-test findings (E-01..E-12)."""

from __future__ import annotations

import ipaddress
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpcore
import httpx
import pytest
import sqlalchemy as sa
from alembic import command as alembic_command
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.conftest import StubSafeHttpClient, make_alembic_config, make_rss_xml

from smartrss.db.models import Feed, Item, Job, PipelineRun, StageMetric, SyncChange
from smartrss.db.recanonicalize import recanonicalize_all_items
from smartrss.db.sync_log import SyncEvent, sync_log
from smartrss.jobs.queue import claim_next_job, enqueue_job
from smartrss.security.safe_http import (
    DomainRateLimiter,
    FetchResult,
    SafeHttpClient,
    SafeTransport,
    SSRFPolicy,
)
from smartrss.worker.runner import execute_claimed_job, run_worker_once
from smartrss.worker.stages.poll_feed import run_poll_feed


def _make_dns(mapping: dict[str, list[str]]) -> Any:
    async def _resolve(
        host: str, _port: int
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        return [ipaddress.ip_address(ip) for ip in mapping.get(host, ["93.184.216.34"])]

    return _resolve


async def test_e01_poll_feed_and_extract_content_typed_paths_on_httpcore_errors(
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E-01 (d): poll_feed and extract_content take typed failure paths (never 'Unhandled error')
    when SafeTransport encounters real httpcore ConnectError/ConnectTimeout/ReadTimeout."""
    import smartrss.worker.runner as runner_mod

    caplog.set_level(logging.ERROR, logger="smartrss.worker.runner")
    unhandled_calls: list[str] = []
    orig_exc = runner_mod.logger.exception

    def _spy_exception(msg: object, *args: object, **kwargs: Any) -> None:
        formatted = str(msg) % args if args else str(msg)
        unhandled_calls.append(formatted)
        orig_exc(msg, *args, **kwargs)

    monkeypatch.setattr(runner_mod.logger, "exception", _spy_exception)

    class _TlsFailStream(httpcore.AsyncNetworkStream):
        async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
            return b""

        async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
            pass

        async def aclose(self) -> None:
            pass

        async def start_tls(
            self,
            ssl_context: Any,
            server_hostname: str | None = None,
            timeout: float | None = None,  # noqa: ASYNC109
        ) -> httpcore.AsyncNetworkStream:
            raise httpcore.ConnectError("TLS handshake failed")

        def get_extra_info(self, info: str) -> Any:
            return None

    class _BodyTimeoutStream(httpcore.AsyncNetworkStream):
        def __init__(self) -> None:
            self._sent_hdr = False

        async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
            if not self._sent_hdr:
                self._sent_hdr = True
                return b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\nhi"
            raise httpcore.ReadTimeout("stream body timed out")

        async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
            pass

        async def aclose(self) -> None:
            pass

        async def start_tls(
            self,
            ssl_context: Any,
            server_hostname: str | None = None,
            timeout: float | None = None,  # noqa: ASYNC109
        ) -> httpcore.AsyncNetworkStream:
            return self

        def get_extra_info(self, info: str) -> Any:
            return None

    class _ErrorBackend(httpcore.AsyncNetworkBackend):
        def __init__(self, mode: str) -> None:
            self.mode = mode

        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,  # noqa: ASYNC109
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            if self.mode == "connect_timeout":
                raise httpcore.ConnectTimeout("connect timed out")
            if self.mode == "connect_error":
                raise httpcore.ConnectError("All connection attempts failed")
            if self.mode == "tls_error":
                return _TlsFailStream()
            return _BodyTimeoutStream()

        async def connect_unix_socket(
            self,
            path: str,
            timeout: float | None = None,  # noqa: ASYNC109
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            raise AssertionError("unreachable")

        async def sleep(self, seconds: float) -> None:
            pass

    feed_tls = uuid.uuid4()
    feed_timeout = uuid.uuid4()
    item_conn = uuid.uuid4()
    item_read_timeout = uuid.uuid4()
    now = datetime.now(UTC)

    async with session_factory() as s:
        s.add(Feed(id=feed_tls, feed_url="https://tls.example.com/rss", title="TLS Feed"))
        s.add(Feed(id=feed_timeout, feed_url="https://slow.example.com/rss", title="Timeout Feed"))
        await s.flush()
        for iid, url in (
            (item_conn, "http://ext-conn.example.com/a1"),
            (item_read_timeout, "http://ext-read.example.com/a2"),
        ):
            s.add(
                Item(
                    id=iid,
                    feed_id=feed_tls,
                    guid=f"g-{iid}",
                    raw_url=url,
                    canonical_url=url,
                    title="Article",
                    fetched_at=now,
                    extraction_status="pending",
                    state="unread",
                    is_starred=False,
                    read_state_updated_at=now,
                    starred_updated_at=now,
                )
            )
        await s.flush()
        await sync_log.record(
            s,
            [
                SyncEvent("feed", feed_tls, "upsert"),
                SyncEvent("feed", feed_timeout, "upsert"),
                SyncEvent("item", item_conn, "upsert"),
                SyncEvent("item", item_read_timeout, "upsert"),
            ],
        )
        j_tls = await enqueue_job(
            s, job_type="poll_feed", dedupe_key=f"feed:{feed_tls}", feed_id=feed_tls
        )
        j_to = await enqueue_job(
            s, job_type="poll_feed", dedupe_key=f"feed:{feed_timeout}", feed_id=feed_timeout
        )
        j_ext_conn = await enqueue_job(
            s,
            job_type="extract_content",
            dedupe_key=f"item:{item_conn}",
            feed_id=feed_tls,
            item_id=item_conn,
        )
        j_ext_rt = await enqueue_job(
            s,
            job_type="extract_content",
            dedupe_key=f"item:{item_read_timeout}",
            feed_id=feed_tls,
            item_id=item_read_timeout,
        )
        await s.commit()

    policy = SSRFPolicy(resolver=_make_dns({}))
    limiter = DomainRateLimiter(min_interval_seconds=0.0)

    # 1. poll_feed TLS ConnectError -> typed _record_feed_poll_error, job done, NO Unhandled error
    async with session_factory() as s:
        job = await claim_next_job(s, worker_id="w-e01", job_types=("poll_feed",), feed_id=feed_tls)
        await s.commit()
    assert job is not None and job.id == j_tls
    async with SafeHttpClient(
        policy=policy, transport=SafeTransport(policy, network_backend=_ErrorBackend("tls_error"))
    ) as client:
        await execute_claimed_job(session_factory, client, limiter, job=job, worker_id="w-e01")

    # 2. poll_feed ConnectTimeout -> typed _record_feed_poll_error, job done, NO Unhandled error
    async with session_factory() as s:
        job = await claim_next_job(
            s, worker_id="w-e01", job_types=("poll_feed",), feed_id=feed_timeout
        )
        await s.commit()
    assert job is not None and job.id == j_to
    async with SafeHttpClient(
        policy=policy,
        transport=SafeTransport(policy, network_backend=_ErrorBackend("connect_timeout")),
    ) as client:
        await execute_claimed_job(session_factory, client, limiter, job=job, worker_id="w-e01")

    # 3. extract_content ConnectError -> TransientExtractionError, NO Unhandled error
    async with session_factory() as s:
        job_c = await claim_next_job(s, worker_id="w-e01", job_types=("extract_content",))
        await s.commit()
    assert job_c is not None and job_c.id == j_ext_conn
    async with SafeHttpClient(
        policy=policy,
        transport=SafeTransport(policy, network_backend=_ErrorBackend("connect_error")),
    ) as client:
        await execute_claimed_job(session_factory, client, limiter, job=job_c, worker_id="w-e01")

    # 4. extract_content ReadTimeout mid-body -> TransientExtractionError, NO Unhandled error
    async with session_factory() as s:
        job_r = await claim_next_job(s, worker_id="w-e01", job_types=("extract_content",))
        await s.commit()
    assert job_r is not None and job_r.id == j_ext_rt
    async with SafeHttpClient(
        policy=policy,
        transport=SafeTransport(policy, network_backend=_ErrorBackend("body_timeout")),
    ) as client:
        await execute_claimed_job(session_factory, client, limiter, job=job_r, worker_id="w-e01")

    # Zero "Unhandled error" logs across all 4 jobs!
    assert unhandled_calls == [], f"Unexpected runner.logger.exception calls: {unhandled_calls}"
    unhandled = [r for r in caplog.records if "Unhandled error" in r.getMessage()]
    assert unhandled == [], f"Unexpected unhandled errors: {[r.getMessage() for r in unhandled]}"

    async with session_factory() as s:
        f_tls = await s.get(Feed, feed_tls)
        f_to = await s.get(Feed, feed_timeout)
        assert f_tls is not None and f_tls.health_state == "degraded"
        assert f_tls.consecutive_errors == 1
        assert f_tls.last_error and "SSRFConnectionError" in f_tls.last_error

        assert f_to is not None and f_to.health_state == "degraded"
        assert f_to.consecutive_errors == 1
        assert f_to.last_error and "SSRFConnectionError" in f_to.last_error

        # Poll jobs completed cleanly via typed error path (not re-raised to runner catch-all)
        jt1 = await s.get(Job, j_tls)
        jt2 = await s.get(Job, j_to)
        assert jt1 is not None and jt1.status == "done"
        assert jt2 is not None and jt2.status == "done"

        # Extraction jobs requeued via TransientExtractionError
        je1 = await s.get(Job, j_ext_conn)
        je2 = await s.get(Job, j_ext_rt)
        assert je1 is not None and je1.status == "queued" and je1.attempts == 1
        assert je1.last_error and "SSRFConnectionError" in je1.last_error
        assert je2 is not None and je2.status == "queued" and je2.attempts == 1
        assert je2.last_error and "SafeHttpTimeoutError" in je2.last_error


async def test_e07_api_responses_include_cache_control_no_store(
    api_env: dict[str, Any],
) -> None:
    """E-07: Backend sends Cache-Control: no-store on API responses."""
    client: httpx.AsyncClient = api_env["client"]
    r_health = await client.get("/api/v1/health")
    assert r_health.status_code == 200
    assert r_health.headers.get("cache-control") == "no-store"

    r_feeds = await client.get("/api/v1/feeds")
    assert r_feeds.status_code == 200
    assert r_feeds.headers.get("cache-control") == "no-store"


async def test_e10_feed_url_validation_all_three_paths(
    api_env: dict[str, Any],
) -> None:
    """E-10: Reject exact soak pasted Markdown URL on create, PATCH, and OPML import."""
    client: httpx.AsyncClient = api_env["client"]
    soak_bad_url = "https://www.latent.space/feed](https://www.latent.space/feed"

    # 1. POST /api/v1/feeds -> 422
    r_create = await client.post("/api/v1/feeds", json={"feed_url": soak_bad_url})
    assert r_create.status_code == 422

    # Positive: valid percent-encoded path & query succeeds
    valid_url = "https://valid.example.com/feed%5D?tag=a%20b"
    r_ok = await client.post("/api/v1/feeds", json={"feed_url": valid_url})
    assert r_ok.status_code == 201
    feed_id = r_ok.json()["id"]

    # 2. PATCH /api/v1/feeds/{id} -> 422 and feed_url unchanged
    r_patch = await client.patch(f"/api/v1/feeds/{feed_id}", json={"feed_url": soak_bad_url})
    assert r_patch.status_code == 422
    r_get = await client.get(f"/api/v1/feeds/{feed_id}")
    assert r_get.status_code == 200
    assert r_get.json()["feed_url"] == valid_url

    # 3. POST /api/v1/feeds/import-opml -> rejected_invalid == 1, imported == 0
    opml_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>Bad Paste</title></head>
  <body>
    <outline text="Latent Space" type="rss" xmlUrl="{soak_bad_url}" />
  </body>
</opml>"""
    r_opml = await client.post("/api/v1/feeds/import-opml", json={"opml_xml": opml_xml})
    assert r_opml.status_code == 200
    body = r_opml.json()
    assert body["imported"] == 0
    assert body["rejected_invalid"] == 1


async def test_e12_recanonicalize_and_poll_feed_guid_rotation_matches(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """E-12: Re-canonicalizing a stored BBC item emits an item upsert and enables
    the N-01 GUID-rotation fallback to match on the next poll without duplicates."""
    feed_id = uuid.uuid4()
    item_id = uuid.uuid4()
    now = datetime.now(UTC)
    raw_bbc = "https://www.bbc.com/news/articles/c3pw4yrr8kz9o?at_medium=RSS&at_campaign=rss"
    old_canon = "https://www.bbc.com/news/articles/c3pw4yrr8kz9o?at_campaign=rss&at_medium=RSS"
    clean_canon = "https://www.bbc.com/news/articles/c3pw4yrr8kz9o"

    async with session_factory() as s:
        s.add(
            Feed(
                id=feed_id,
                feed_url="https://feeds.bbci.co.uk/news/world/rss.xml",
                title="BBC World",
                initial_backfill_done=True,
                fetch_full_text=False,
            )
        )
        await s.flush()
        s.add(
            Item(
                id=item_id,
                feed_id=feed_id,
                guid="https://www.bbc.com/news/articles/c3pw4yrr8kz9o#0",
                raw_url=raw_bbc,
                canonical_url=old_canon,
                title="BBC Headline",
                raw_summary="Initial summary",
                entry_hash="old-hash",
                content_hash="old-hash",
                fetched_at=now,
                extraction_status="fallback_rss",
                state="unread",
                is_starred=False,
                read_state_updated_at=now,
                starred_updated_at=now,
            )
        )
        await s.flush()
        await sync_log.record(
            s,
            [SyncEvent("feed", feed_id, "upsert"), SyncEvent("item", item_id, "upsert")],
        )
        await s.commit()

    # Run recanonicalize_all_items
    updated = await recanonicalize_all_items(session_factory)
    assert updated == 1
    # Idempotent on second run
    assert await recanonicalize_all_items(session_factory) == 0

    async with session_factory() as s:
        item = await s.get(Item, item_id)
        assert item is not None
        assert item.canonical_url == clean_canon
        latest_change = (
            await s.execute(sa.select(SyncChange).order_by(SyncChange.seq.desc()).limit(1))
        ).scalar_one()
        assert latest_change.entity_type == "item"
        assert latest_change.entity_id == item_id
        assert latest_change.op == "upsert"

    # Poll feed with GUID rotated #0 -> #1 carrying BBC tracking params -> matches existing item!
    rss_rotated = make_rss_xml(
        "BBC World",
        [
            {
                "title": "BBC Headline Updated",
                "link": raw_bbc,
                "guid": "https://www.bbc.com/news/articles/c3pw4yrr8kz9o#1",
                "summary": "Updated summary",
            }
        ],
    )
    stub = StubSafeHttpClient(
        lambda url, _hdrs: FetchResult(200, url, None, {}, rss_rotated, "application/rss+xml")
    )
    outcome = await run_poll_feed(session_factory, stub, feed_id=feed_id)
    assert outcome.metric_status == "ok"
    assert outcome.outcome == "fetched"
    assert outcome.items_ingested == 0
    assert outcome.items_updated == 1

    async with session_factory() as s:
        items = (await s.execute(sa.select(Item).where(Item.feed_id == feed_id))).scalars().all()
        assert len(items) == 1
        assert items[0].id == item_id
        assert items[0].title == "BBC Headline Updated"


async def test_e08_e06_304_outcome_metrics_rollup_and_run_once_counters(
    api_env: dict[str, Any],
    db_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """E-06 & E-08: 304 polls record outcome='not_modified' + status='ok' + http_status=304,
    200 polls record outcome='fetched' + http_status=200, rollup includes not_modified_count
    while ok_count still includes 304s, and run-once / pipeline_runs report feeds_polled &
    feeds_not_modified alongside items_ingested / items_polled."""
    client: httpx.AsyncClient = api_env["client"]

    r1 = await client.post(
        "/api/v1/feeds",
        json={"feed_url": "https://fetched.example.com/rss", "fetch_full_text": False},
    )
    r2 = await client.post(
        "/api/v1/feeds",
        json={"feed_url": "https://notmod.example.com/rss", "fetch_full_text": False},
    )
    assert r1.status_code == 201 and r2.status_code == 201
    f_fetched = uuid.UUID(r1.json()["id"])
    f_304 = uuid.UUID(r2.json()["id"])

    rss_xml = make_rss_xml(
        "Fetched Feed",
        [
            {
                "title": "New Entry",
                "link": "https://fetched.example.com/post-1",
                "guid": "p-1",
                "summary": "Body",
            }
        ],
    )

    def _handler(url: str, _hdrs: dict[str, str]) -> FetchResult:
        if "notmod.example.com" in url:
            return FetchResult(304, url, None, {"etag": '"v2"'}, b"", None)
        return FetchResult(200, url, None, {"etag": '"v1"'}, rss_xml, "application/rss+xml")

    summary = await run_worker_once(
        db_engine,
        session_factory,
        StubSafeHttpClient(_handler),
        stage="poll",
    )
    assert summary.status == "completed"
    assert summary.exit_code == 0
    assert summary.feeds_polled == 2
    assert summary.feeds_not_modified == 1
    assert summary.items_ingested == 1
    assert summary.items_polled == 1
    summary_dict = summary.to_dict()
    assert summary_dict["feeds_polled"] == 2
    assert summary_dict["feeds_not_modified"] == 1
    assert summary_dict["items_ingested"] == 1
    assert summary_dict["items_polled"] == 1

    # Verify stage_metrics rows directly
    async with session_factory() as s:
        m_rows = (
            (await s.execute(sa.select(StageMetric).where(StageMetric.job_type == "poll_feed")))
            .scalars()
            .all()
        )
        by_feed = {m.feed_id: m for m in m_rows}
        assert by_feed[f_fetched].status == "ok"
        assert by_feed[f_fetched].outcome == "fetched"
        assert by_feed[f_fetched].http_status == 200
        assert by_feed[f_304].status == "ok"
        assert by_feed[f_304].outcome == "not_modified"
        assert by_feed[f_304].http_status == 304

        assert summary.run_id is not None
        run_row = await s.get(PipelineRun, uuid.UUID(summary.run_id))
        assert run_row is not None
        assert run_row.feeds_polled == 2
        assert run_row.feeds_not_modified == 1
        assert run_row.items_polled == 1

    # Verify /api/v1/system/metrics rollup: ok_count includes 304s, not_modified_count == 1
    r_metrics = await client.get("/api/v1/system/metrics?window=24h")
    assert r_metrics.status_code == 200
    stages = {st["job_type"]: st for st in r_metrics.json()["stages"]}
    poll_stats = stages["poll_feed"]
    assert poll_stats["total_count"] == 2
    assert poll_stats["ok_count"] == 2
    assert poll_stats["not_modified_count"] == 1
    assert poll_stats["failed_count"] == 0

    # Verify /api/v1/system/health recent_runs exposes feeds_polled & feeds_not_modified
    r_health = await client.get("/api/v1/system/health")
    assert r_health.status_code == 200
    latest_run = r_health.json()["recent_runs"][0]
    assert latest_run["feeds_polled"] == 2
    assert latest_run["feeds_not_modified"] == 1
    assert latest_run["items_polled"] == 1


async def test_e08_e12_migration_0003_upgrade_downgrade_on_populated_db(
    test_db_url: str,
    db_engine: AsyncEngine,
) -> None:
    """E-08 (c) & E-12: Migration 0003 upgrades a populated 0002 database (adding outcome,
    http_status, feed counters and recanonicalizing BBC URLs + sync_changes) and downgrades."""
    scratch_db = f"smartrss_mig0003_{uuid.uuid4().hex[:8]}"
    base_url = test_db_url.rsplit("/", 1)[0]
    admin_url = f"{base_url}/postgres"
    scratch_url = f"{base_url}/{scratch_db}"

    admin_engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{scratch_db}"'))

        cfg = make_alembic_config(scratch_url)
        alembic_command.upgrade(cfg, "0002_milestone_1b_sync_search")

        # Populate 0002 tables with a BBC item (old canonical_url), pipeline_run, and stage_metrics
        sync_engine = sa.create_engine(scratch_url)
        feed_id = uuid.uuid4()
        item_id = uuid.uuid4()
        run_id = uuid.uuid4()
        raw_bbc = "https://www.bbc.com/news/articles/c3pw4yrr8kz9o?at_medium=RSS&at_campaign=rss"
        old_canon = "https://www.bbc.com/news/articles/c3pw4yrr8kz9o?at_campaign=rss&at_medium=RSS"
        try:
            with sync_engine.begin() as conn:
                conn.execute(
                    sa.text(
                        """
                        INSERT INTO feeds (id, feed_url, title)
                        VALUES (:id, :url, 'BBC World')
                        """
                    ),
                    {"id": feed_id, "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
                )
                conn.execute(
                    sa.text(
                        """
                        INSERT INTO items (
                            id, feed_id, guid, raw_url, canonical_url, title,
                            fetched_at, read_state_updated_at, starred_updated_at
                        )
                        VALUES (
                            :id, :fid, 'bbc#0', :raw_url, :canon, 'BBC Story',
                            now(), now(), now()
                        )
                        """
                    ),
                    {"id": item_id, "fid": feed_id, "raw_url": raw_bbc, "canon": old_canon},
                )
                conn.execute(
                    sa.text(
                        """
                        INSERT INTO pipeline_runs (id, trigger, stages, status)
                        VALUES (:id, 'cron_cli', ARRAY['poll_feed'], 'completed')
                        """
                    ),
                    {"id": run_id},
                )
                conn.execute(
                    sa.text(
                        """
                        INSERT INTO stage_metrics (
                            run_id, job_type, feed_id, status, queue_wait_ms, duration_ms
                        )
                        VALUES (:rid, 'poll_feed', :fid, 'ok', 10, 250)
                        """
                    ),
                    {"rid": run_id, "fid": feed_id},
                )

            # Upgrade to head (0003)
            alembic_command.upgrade(cfg, "head")

            with sync_engine.begin() as conn:
                # BBC canonical_url was stripped and sync_changes item upsert emitted
                new_canon = conn.execute(
                    sa.text("SELECT canonical_url FROM items WHERE id = :id"), {"id": item_id}
                ).scalar_one()
                assert new_canon == "https://www.bbc.com/news/articles/c3pw4yrr8kz9o"

                seq = conn.execute(
                    sa.text("SELECT current_seq FROM sync_state WHERE id = 1")
                ).scalar_one()
                assert seq == 1
                sc = conn.execute(
                    sa.text("SELECT entity_type, entity_id, op FROM sync_changes WHERE seq = 1")
                ).one()
                assert (sc.entity_type, sc.entity_id, sc.op) == ("item", item_id, "upsert")

                # New columns exist with expected defaults & check constraint enforced
                run_cols = conn.execute(
                    sa.text(
                        "SELECT feeds_polled, feeds_not_modified FROM pipeline_runs WHERE id = :id"
                    ),
                    {"id": run_id},
                ).one()
                assert (run_cols.feeds_polled, run_cols.feeds_not_modified) == (0, 0)

                conn.execute(
                    sa.text(
                        """
                        INSERT INTO stage_metrics (
                            run_id, job_type, feed_id, status, outcome, http_status,
                            queue_wait_ms, duration_ms
                        )
                        VALUES (:rid, 'poll_feed', :fid, 'ok', 'not_modified', 304, 5, 80)
                        """
                    ),
                    {"rid": run_id, "fid": feed_id},
                )

            with pytest.raises(IntegrityError), sync_engine.begin() as conn:
                conn.execute(
                    sa.text(
                        """
                        INSERT INTO stage_metrics (
                            run_id, job_type, status, outcome, queue_wait_ms, duration_ms
                        )
                        VALUES (:rid, 'poll_feed', 'ok', 'invalid_outcome', 1, 1)
                        """
                    ),
                    {"rid": run_id},
                )

            # Downgrade cleanly back to 0002 on populated table, then upgrade back to head
            alembic_command.downgrade(cfg, "0002_milestone_1b_sync_search")
            alembic_command.upgrade(cfg, "head")
        finally:
            sync_engine.dispose()
    finally:
        with admin_engine.connect() as conn:
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :db AND pid <> pg_backend_pid()"
                ),
                {"db": scratch_db},
            )
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
        admin_engine.dispose()
