"""Integration tests for feed/category API, OPML, and polite feed polling edge cases (§12.1)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import StubSafeHttpClient, make_rss_xml

from smartrss.cli import update_app_setting
from smartrss.db.models import Feed, Item, Job, SyncChange
from smartrss.db.sync_log import APP_SETTINGS_ENTITY_ID, SyncEvent, execute_synced, sync_log
from smartrss.security.safe_http import FetchResult
from smartrss.worker.stages.poll_feed import (
    record_poll_feed_unexpected_failure,
    run_poll_feed,
)


async def test_category_crud_delete_stamps_child_feed_lww(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client: httpx.AsyncClient = api_env["client"]

    r_cat = await client.post("/api/v1/categories", json={"name": "Tech", "sort_order": 1})
    assert r_cat.status_code == 201
    cat_id = r_cat.json()["id"]

    # Case-insensitive duplicate rejected with 409
    r_dup = await client.post("/api/v1/categories", json={"name": "tech"})
    assert r_dup.status_code == 409

    # Subscribe feed in that category
    r_feed = await client.post(
        "/api/v1/feeds",
        json={"feed_url": "https://tech.example.com/rss.xml", "category_id": cat_id},
    )
    assert r_feed.status_code == 201
    feed_id = uuid.UUID(r_feed.json()["id"])

    # Rename category
    r_patch = await client.patch(f"/api/v1/categories/{cat_id}", json={"name": "Systems"})
    assert r_patch.status_code == 200
    assert r_patch.json()["name"] == "Systems"

    # Soft-delete category -> clears child category_id & stamps field_updated_at['category_id']
    r_del = await client.delete(f"/api/v1/categories/{cat_id}")
    assert r_del.status_code == 204

    async with session_factory() as s:
        feed = await s.get(Feed, feed_id)
        assert feed is not None
        assert feed.category_id is None
        assert "category_id" in feed.field_updated_at

        changes = (
            (await s.execute(sa.select(SyncChange).order_by(SyncChange.seq.desc()).limit(2)))
            .scalars()
            .all()
        )
        ops = {(c.entity_type, c.op) for c in changes}
        assert ("category", "delete") in ops
        assert ("feed", "upsert") in ops


async def test_feed_subscribe_title_preservation_patch_and_410_recovery(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client: httpx.AsyncClient = api_env["client"]

    # SSRF URL rejected at subscribe time (422)
    r_ssrf = await client.post("/api/v1/feeds", json={"feed_url": "http://internal.local/rss"})
    assert r_ssrf.status_code == 422

    # 1. Subscribe WITHOUT user title -> auto-derived title unstamped, poller fills feed <title>
    r_auto = await client.post("/api/v1/feeds", json={"feed_url": "https://auto.example.com/rss"})
    assert r_auto.status_code == 201
    auto_id = uuid.UUID(r_auto.json()["id"])
    assert "title" not in r_auto.json()["field_updated_at"]

    # 2. Subscribe WITH user title -> stamped in field_updated_at, poller must preserve it
    r_named = await client.post(
        "/api/v1/feeds",
        json={"feed_url": "https://named.example.com/rss", "title": "My Custom Name"},
    )
    assert r_named.status_code == 201
    named_id = uuid.UUID(r_named.json()["id"])
    assert "title" in r_named.json()["field_updated_at"]

    rss_bytes = make_rss_xml(
        "Server Feed Title",
        [{"title": "Post 1", "link": "https://auto.example.com/1", "guid": "p1"}],
    )
    http_stub = StubSafeHttpClient(
        lambda url, _hdrs: FetchResult(
            status_code=200,
            final_url=url,
            permanent_redirect_url=None,
            headers={"etag": '"v1"', "last-modified": "Wed, 25 Nov 2026 00:00:00 GMT"},
            body_bytes=rss_bytes,
            content_type="application/rss+xml",
        )
    )
    await run_poll_feed(session_factory, http_stub, feed_id=auto_id)
    await run_poll_feed(session_factory, http_stub, feed_id=named_id)

    async with session_factory() as s:
        f_auto = await s.get(Feed, auto_id)
        f_named = await s.get(Feed, named_id)
        assert f_auto is not None and f_auto.title == "Server Feed Title"
        assert f_named is not None and f_named.title == "My Custom Name"

    # PATCH curation_instructions enforces optimistic expected_config_version
    r_conflict = await client.patch(
        f"/api/v1/feeds/{auto_id}",
        json={"curation_instructions": "Focus on Rust", "expected_config_version": 99},
    )
    assert r_conflict.status_code == 409

    r_ok_ci = await client.patch(
        f"/api/v1/feeds/{auto_id}",
        json={"curation_instructions": "Focus on Rust", "expected_config_version": 1},
    )
    assert r_ok_ci.status_code == 200
    assert r_ok_ci.json()["config_version"] == 2

    # Editing structured field does NOT increment config_version, DOES stamp field_updated_at
    r_rename = await client.patch(f"/api/v1/feeds/{auto_id}", json={"title": "Renamed By User"})
    assert r_rename.status_code == 200
    assert r_rename.json()["config_version"] == 2
    assert "title" in r_rename.json()["field_updated_at"]

    # 410 Gone transitions health_state='gone', next_fetch_at=NULL
    http_410 = StubSafeHttpClient(
        lambda url, _hdrs: FetchResult(410, url, None, {}, b"", "text/plain")
    )
    res_410 = await run_poll_feed(session_factory, http_410, feed_id=auto_id)
    assert res_410.metric_status == "failed"

    async with session_factory() as s:
        f_auto = await s.get(Feed, auto_id)
        assert f_auto is not None and f_auto.health_state == "gone" and f_auto.next_fetch_at is None

    # PATCH feed_url collision -> 409
    r_url_409 = await client.patch(
        f"/api/v1/feeds/{auto_id}",
        json={"feed_url": "https://named.example.com/rss"},
    )
    assert r_url_409.status_code == 409

    # PATCH feed_url valid URL -> recovers gone->healthy, clears etag/last_modified, enqueues poll
    r_recover = await client.patch(
        f"/api/v1/feeds/{auto_id}",
        json={"feed_url": "https://auto-new.example.com/rss"},
    )
    assert r_recover.status_code == 200
    assert r_recover.json()["health_state"] == "healthy"
    assert r_recover.json()["initial_backfill_done"] is False


async def test_304_diff_gating_301_migration_and_collision_429_and_unexpected_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    f1_id = uuid.uuid4()
    f2_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(
            Feed(
                id=f1_id,
                feed_url="https://f1.example.com/rss",
                title="Feed 1",
                etag='"e1"',
                initial_backfill_done=True,
            )
        )
        s.add(
            Feed(
                id=f2_id,
                feed_url="https://f2.example.com/rss",
                title="Feed 2",
                initial_backfill_done=True,
            )
        )
        await s.flush()
        await sync_log.record(
            s, [SyncEvent("feed", f1_id, "upsert"), SyncEvent("feed", f2_id, "upsert")]
        )
        await s.commit()

    # 1. Steady-state 304 emits ZERO feed sync_changes
    async with session_factory() as s:
        seq_before = (await s.execute(sa.select(sa.func.max(SyncChange.seq)))).scalar_one()

    stub_304 = StubSafeHttpClient(
        lambda url, hdrs: FetchResult(304, url, None, {"cache-control": "max-age=7200"}, b"", None)
    )
    res = await run_poll_feed(session_factory, stub_304, feed_id=f1_id)
    assert res.metric_status == "ok"
    assert stub_304.calls[0][1].get("If-None-Match") == '"e1"'

    async with session_factory() as s:
        seq_after = (await s.execute(sa.select(sa.func.max(SyncChange.seq)))).scalar_one()
        assert seq_after == seq_before

    # 2. 429 with Retry-After degrades health & schedules backoff
    stub_429 = StubSafeHttpClient(
        lambda url, _hdrs: FetchResult(429, url, None, {"retry-after": "600"}, b"", None)
    )
    await run_poll_feed(session_factory, stub_429, feed_id=f1_id)
    async with session_factory() as s:
        f1 = await s.get(Feed, f1_id)
        assert f1 is not None and f1.health_state == "degraded" and f1.consecutive_errors == 1
        assert f1.next_fetch_at is not None

    # 3. Recovery via 304 (degraded -> healthy) DOES emit a feed upsert
    async with session_factory() as s:
        seq_before_rec = (await s.execute(sa.select(sa.func.max(SyncChange.seq)))).scalar_one()
    await run_poll_feed(session_factory, stub_304, feed_id=f1_id)
    async with session_factory() as s:
        seq_after_rec = (await s.execute(sa.select(sa.func.max(SyncChange.seq)))).scalar_one()
        assert seq_after_rec == seq_before_rec + 1
        f1 = await s.get(Feed, f1_id)
        assert f1 is not None and f1.health_state == "healthy" and f1.consecutive_errors == 0

    # 4. 301 normal migration updates feed_url
    stub_301_ok = StubSafeHttpClient(
        lambda _url, _hdrs: FetchResult(
            200,
            "https://f1-moved.example.com/rss",
            "https://f1-moved.example.com/rss",
            {},
            make_rss_xml("Feed 1", []),
            "application/rss+xml",
        )
    )
    await run_poll_feed(session_factory, stub_301_ok, feed_id=f1_id)
    async with session_factory() as s:
        f1 = await s.get(Feed, f1_id)
        assert f1 is not None and f1.feed_url == "https://f1-moved.example.com/rss"

    # 5. 301 collision with f2 sets health_state='broken' without crashing
    stub_301_collide = StubSafeHttpClient(
        lambda _url, _hdrs: FetchResult(
            200,
            "https://f2.example.com/rss",
            "https://f2.example.com/rss",
            {},
            make_rss_xml("Feed 2", []),
            "application/rss+xml",
        )
    )
    res_col = await run_poll_feed(session_factory, stub_301_collide, feed_id=f1_id)
    assert res_col.metric_status == "failed"
    async with session_factory() as s:
        f1 = await s.get(Feed, f1_id)
        assert f1 is not None
        assert f1.feed_url == "https://f1-moved.example.com/rss"
        assert f1.health_state == "broken"
        assert "collides" in (f1.last_error or "").lower()

    # 6. Unexpected exception hook degrades health and advances next_fetch_at
    before_err = datetime.now(UTC)
    await record_poll_feed_unexpected_failure(session_factory, f2_id, "RuntimeError: boom")
    async with session_factory() as s:
        f2 = await s.get(Feed, f2_id)
        assert f2 is not None and f2.health_state == "degraded" and f2.consecutive_errors == 1
        assert f2.next_fetch_at is not None and f2.next_fetch_at > before_err


async def test_backfill_cap_muted_feed_reactivation_same_link_and_entry_hash(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client: httpx.AsyncClient = api_env["client"]

    # Configure backfill_cap via update_app_setting -> emits app_settings upsert
    async with session_factory() as s:
        await update_app_setting(s, key="backfill_cap", value=3)
        await s.commit()
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

    # Subscribe feed & poll 5 entries + 1 duplicate GUID -> top 3 unread (prio 90), 2 expired
    r_sub = await client.post("/api/v1/feeds", json={"feed_url": "https://cap.example.com/rss"})
    feed_id = uuid.UUID(r_sub.json()["id"])

    entries_5 = [
        {
            "title": f"Post {i}",
            "link": f"https://cap.example.com/post/{i}",
            "guid": f"guid-{i}",
            "summary": f"Summary {i}",
            "pubDate": f"Wed, {20 + i} Nov 2026 12:00:00 GMT",
        }
        for i in range(1, 6)
    ]
    # Add intra-batch duplicate GUID
    entries_5.append(dict(entries_5[0]))

    stub_init = StubSafeHttpClient(
        lambda url, _hdrs: FetchResult(
            200,
            url,
            None,
            {"etag": '"v1"'},
            make_rss_xml("Cap Feed", entries_5),
            "application/rss+xml",
        )
    )
    out = await run_poll_feed(session_factory, stub_init, feed_id=feed_id)
    assert out.items_ingested == 5

    async with session_factory() as s:
        items = (await s.execute(sa.select(Item).where(Item.feed_id == feed_id))).scalars().all()
        unread = [i for i in items if i.state == "unread"]
        expired = [i for i in items if i.state == "expired"]
        assert len(unread) == 3
        assert len(expired) == 2
        for ex in expired:
            assert ex.expired_at == ex.fetched_at
            assert ex.read_state_updated_at == ex.fetched_at
            assert ex.extraction_status == "fallback_rss"

        ext_jobs = (
            (
                await s.execute(
                    sa.select(Job).where(Job.job_type == "extract_content", Job.feed_id == feed_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(ext_jobs) == 3
        assert all(j.priority == 90 for j in ext_jobs)

    # Simulate extraction updating extracted_text & content_hash (NOT entry_hash) on one unread item
    # and mark another unread item 'read'
    target_unread = unread[0]
    target_read = unread[1]
    async with session_factory() as s:
        await execute_synced(
            s,
            sa.update(Item)
            .where(Item.id == target_unread.id)
            .values(
                extracted_text="Full extracted body text...",
                extraction_status="ok",
                content_hash="different-extracted-content-hash",
            ),
            lambda _res: [SyncEvent("item", target_unread.id, "upsert")],
        )
        await execute_synced(
            s,
            sa.update(Item)
            .where(Item.id == target_read.id)
            .values(state="read", read_at=datetime.now(UTC)),
            lambda _res: [SyncEvent("item", target_read.id, "upsert")],
        )
        await sync_log.record(
            s,
            [
                SyncEvent("item", target_unread.id, "upsert"),
                SyncEvent("item", target_read.id, "upsert"),
            ],
        )
        await s.commit()

    # Next unchanged poll: entry_hash matches, so extracted unread item is NOT re-extracted!
    out_unchanged = await run_poll_feed(session_factory, stub_init, feed_id=feed_id)
    assert out_unchanged.items_updated == 0
    async with session_factory() as s:
        u_after = await s.get(Item, target_unread.id)
        assert u_after is not None and u_after.extraction_status == "ok"

    # Next poll edits BOTH target_unread and target_read feed summaries:
    # target_unread updates & resets to pending; target_read stays 'read' and untouched!
    edited_entries = []
    for e in entries_5[:5]:
        copy_e = dict(e)
        if copy_e["guid"] in (target_unread.guid, target_read.guid):
            copy_e["summary"] = "Updated RSS summary text!"
        edited_entries.append(copy_e)

    stub_edited = StubSafeHttpClient(
        lambda url, _hdrs: FetchResult(
            200, url, None, {}, make_rss_xml("Cap Feed", edited_entries), "application/rss+xml"
        )
    )
    out_edit = await run_poll_feed(session_factory, stub_edited, feed_id=feed_id)
    assert out_edit.items_updated == 1
    async with session_factory() as s:
        u_after = await s.get(Item, target_unread.id)
        r_after = await s.get(Item, target_read.id)
        assert u_after is not None and u_after.extraction_status == "pending"
        assert u_after.raw_summary == "Updated RSS summary text!"
        assert r_after is not None and r_after.state == "read"

    # Unsubscribe -> re-subscribe clears etag, keeps read/expired state, dedupes existing items
    r_unsub = await client.patch(f"/api/v1/feeds/{feed_id}", json={"status": "unsubscribed"})
    assert r_unsub.status_code == 200

    r_resub = await client.post("/api/v1/feeds", json={"feed_url": "https://cap.example.com/rss"})
    assert r_resub.status_code == 201
    assert r_resub.json()["status"] == "active"
    assert r_resub.json()["initial_backfill_done"] is False

    async with session_factory() as s:
        f_re = await s.get(Feed, feed_id)
        assert f_re is not None and f_re.etag is None

    # Re-polling returns existing items + 1 new item -> read/expired unchanged, only new inserted
    resub_entries = [
        *edited_entries,
        {
            "title": "Post 6",
            "link": "https://cap.example.com/post/6",
            "guid": "guid-6",
            "summary": "Six",
        },
    ]
    stub_resub = StubSafeHttpClient(
        lambda url, _hdrs: FetchResult(
            200, url, None, {}, make_rss_xml("Cap Feed", resub_entries), "application/rss+xml"
        )
    )
    out_resub = await run_poll_feed(session_factory, stub_resub, feed_id=feed_id)
    assert out_resub.items_ingested == 1
    async with session_factory() as s:
        r_still = await s.get(Item, target_read.id)
        assert r_still is not None and r_still.state == "read"

    # Muted feed auto-marks new items as read at ingest time
    await client.patch(f"/api/v1/feeds/{feed_id}", json={"status": "muted"})
    muted_entries = [
        *resub_entries,
        {
            "title": "Muted Post",
            "link": "https://cap.example.com/post/7",
            "guid": "guid-7",
            "summary": "Quiet",
        },
    ]
    stub_muted = StubSafeHttpClient(
        lambda url, _hdrs: FetchResult(
            200, url, None, {}, make_rss_xml("Cap Feed", muted_entries), "application/rss+xml"
        )
    )
    await run_poll_feed(session_factory, stub_muted, feed_id=feed_id)
    async with session_factory() as s:
        muted_item = (
            await s.execute(sa.select(Item).where(Item.feed_id == feed_id, Item.guid == "guid-7"))
        ).scalar_one()
        assert muted_item.state == "read"
        assert muted_item.read_at == muted_item.fetched_at
        assert muted_item.read_state_updated_at == muted_item.fetched_at


async def test_same_link_feed_vs_guarded_unstable_guid_fallback(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """N-01: same-link entries across polls are never swallowed; genuine GUID change matches."""
    feed_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(
            Feed(
                id=feed_id,
                feed_url="https://comic.example.com/rss",
                title="Daily Comic",
                initial_backfill_done=True,
                fetch_full_text=False,
            )
        )
        await s.flush()
        await sync_log.record(s, [SyncEvent("feed", feed_id, "upsert")])
        await s.commit()

    # Poll 1: entry with guid "ep-1" and shared link /latest, plus entry with guid "only-1"
    poll1 = [
        {
            "title": "Episode 1",
            "link": "https://comic.example.com/latest",
            "guid": "ep-1",
            "summary": "Ep1",
        },
        {
            "title": "Unique Post",
            "link": "https://comic.example.com/unique",
            "guid": "old-guid",
            "summary": "Orig",
        },
    ]
    await run_poll_feed(
        session_factory,
        StubSafeHttpClient(
            lambda u, _h: FetchResult(
                200, u, None, {}, make_rss_xml("Daily Comic", poll1), "application/rss+xml"
            )
        ),
        feed_id=feed_id,
    )

    # Poll 2:
    # - ep-1 STILL present AND new ep-2 shares /latest -> ep-2 MUST be inserted as NEW!
    # - unique post changed guid (old-guid absent, 1 existing, 1 incoming) -> matches via fallback!
    poll2 = [
        {
            "title": "Episode 2",
            "link": "https://comic.example.com/latest",
            "guid": "ep-2",
            "summary": "Ep2",
        },
        {
            "title": "Episode 1",
            "link": "https://comic.example.com/latest",
            "guid": "ep-1",
            "summary": "Ep1",
        },
        {
            "title": "Unique Post",
            "link": "https://comic.example.com/unique",
            "guid": "new-guid",
            "summary": "Orig",
        },
    ]
    out2 = await run_poll_feed(
        session_factory,
        StubSafeHttpClient(
            lambda u, _h: FetchResult(
                200, u, None, {}, make_rss_xml("Daily Comic", poll2), "application/rss+xml"
            )
        ),
        feed_id=feed_id,
    )
    assert out2.items_ingested == 1  # only ep-2 inserted; unique post matched via fallback!

    async with session_factory() as s:
        items = (
            (await s.execute(sa.select(Item).where(Item.feed_id == feed_id).order_by(Item.guid)))
            .scalars()
            .all()
        )
        assert {i.guid for i in items} == {"ep-1", "ep-2", "old-guid"}


async def test_concurrent_patch_preserved_during_poll_and_opml_import_export(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client: httpx.AsyncClient = api_env["client"]

    # OPML import with valid feeds, category, duplicate, and SSRF URL
    opml_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <opml version="2.0">
      <head><title>Subs</title></head>
      <body>
        <outline text="News" title="News">
          <outline type="rss" text="Alpha News" xmlUrl="https://alpha.example.com/rss"/>
          <outline type="rss" text="Alpha Dup" xmlUrl="https://alpha.example.com/rss"/>
          <outline type="rss" text="Evil LAN" xmlUrl="http://192.168.1.1/rss"/>
        </outline>
      </body>
    </opml>"""

    r_imp = await client.post("/api/v1/feeds/import-opml", json={"opml_xml": opml_xml})
    assert r_imp.status_code == 200
    assert r_imp.json() == {
        "imported": 1,
        "reactivated": 0,
        "skipped_duplicates": 1,
        "rejected_invalid": 1,
    }

    r_exp = await client.get("/api/v1/feeds/export-opml")
    assert r_exp.status_code == 200
    assert "https://alpha.example.com/rss" in r_exp.text
    assert "Alpha News" in r_exp.text

    # Verify concurrent PATCH during poll HTTP I/O is NOT overwritten by column-scoped poll update
    feeds = (await client.get("/api/v1/feeds")).json()
    feed_id = uuid.UUID(feeds[0]["id"])

    in_http = asyncio.Event()
    release_http = asyncio.Event()

    class SlowStubHttp(StubSafeHttpClient):
        async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            in_http.set()
            await release_http.wait()
            return FetchResult(
                200,
                url,
                None,
                {},
                make_rss_xml("Server Title", []),
                "application/rss+xml",
            )

    slow_http = SlowStubHttp(lambda u, _h: FetchResult(200, u, None, {}, b"", None))
    poll_task = asyncio.create_task(run_poll_feed(session_factory, slow_http, feed_id=feed_id))
    await in_http.wait()

    # Concurrently PATCH curation_instructions & expiration_days_override while HTTP is in flight
    r_patch = await client.patch(
        f"/api/v1/feeds/{feed_id}",
        json={
            "expiration_days_override": 7,
            "curation_instructions": "Prioritize benchmarks",
            "expected_config_version": 1,
        },
    )
    assert r_patch.status_code == 200

    release_http.set()
    await poll_task

    r_get = await client.get(f"/api/v1/feeds/{feed_id}")
    data = r_get.json()
    assert data["expiration_days_override"] == 7
    assert data["curation_instructions"] == "Prioritize benchmarks"
    assert data["title"] == "Alpha News"  # OPML user title preserved
    assert data["initial_backfill_done"] is True

    # Manual poll endpoint & feed health dashboard & items listing
    r_poll = await client.post(f"/api/v1/feeds/{feed_id}/poll")
    assert r_poll.status_code == 200
    assert r_poll.json()["queued"] is True

    r_health = await client.get("/api/v1/feeds/health")
    assert r_health.status_code == 200
    assert len(r_health.json()) == 1

    r_items = await client.get("/api/v1/items")
    assert r_items.status_code == 200


async def test_poller_edge_cases_skipped_errors_malformed_xml_and_fallback_guid(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from smartrss.security.safe_http import SSRFBlockedError

    paused_id = uuid.uuid4()
    err_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(
            Feed(
                id=paused_id,
                feed_url="https://paused.example.com/rss",
                title="Paused",
                status="paused",
            )
        )
        s.add(
            Feed(
                id=err_id,
                feed_url="https://err.example.com/rss",
                title="Err Feed",
                fetch_full_text=False,
            )
        )
        await s.flush()
        await sync_log.record(
            s, [SyncEvent("feed", paused_id, "upsert"), SyncEvent("feed", err_id, "upsert")]
        )
        await s.commit()

    stub_ok = StubSafeHttpClient(lambda u, _h: FetchResult(200, u, None, {}, b"", None))
    # Paused feed skipped without HTTP fetch
    assert (
        await run_poll_feed(session_factory, stub_ok, feed_id=paused_id)
    ).metric_status == "skipped"
    assert stub_ok.calls == []

    # SSRF error degrades feed health
    stub_ssrf = StubSafeHttpClient(lambda _u, _h: SSRFBlockedError("private IP"))
    assert (
        await run_poll_feed(session_factory, stub_ssrf, feed_id=err_id)
    ).metric_status == "failed"

    # HTTP 500 degrades feed health further
    stub_500 = StubSafeHttpClient(lambda u, _h: FetchResult(500, u, None, {}, b"err", "text/plain"))
    assert (
        await run_poll_feed(session_factory, stub_500, feed_id=err_id)
    ).metric_status == "failed"

    # Malformed non-XML payload degrades feed health
    stub_bozo = StubSafeHttpClient(
        lambda u, _h: FetchResult(200, u, None, {}, b"<<<not xml at all", "application/xml")
    )
    assert (
        await run_poll_feed(session_factory, stub_bozo, feed_id=err_id)
    ).metric_status == "failed"

    async with session_factory() as s:
        f = await s.get(Feed, err_id)
        assert f is not None and f.consecutive_errors == 3 and f.health_state == "degraded"

    # Entry without <guid>, with non-HTTP link skipped, and longer content:encoded preferred
    atom_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Atom Feed</title>
      <link href="https://err.example.com"/>
      <entry>
        <title>No ID Entry</title>
        <link href="https://err.example.com/no-id"/>
        <summary>Short</summary>
        <content type="html">Much longer atom content body than the summary</content>
      </entry>
      <entry>
        <title>Bad Scheme</title>
        <link href="javascript:alert(1)"/>
      </entry>
    </feed>"""
    stub_atom = StubSafeHttpClient(
        lambda u, _h: FetchResult(200, u, None, {}, atom_xml, "application/atom+xml")
    )
    out = await run_poll_feed(session_factory, stub_atom, feed_id=err_id)
    assert out.metric_status == "ok"
    assert out.items_ingested == 1

    async with session_factory() as s:
        item = (await s.execute(sa.select(Item).where(Item.feed_id == err_id))).scalar_one()
        assert item.guid.startswith("fallback:")
        assert item.raw_summary == "Much longer atom content body than the summary"
