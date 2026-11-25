"""Proven regression tests for Milestone 1b implementation review findings (K-01..K-04, K-07)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.conftest import StubSafeHttpClient, make_rss_xml

import smartrss.sync.service as sync_service_mod
from smartrss.db.models import Category, Feed, Item, SyncState
from smartrss.db.sync_log import SyncEvent, bypass_sync_guard, sync_log
from smartrss.search.fts import (
    SearchVectorInput,
    rebuild_item_search_vector,
    rebuild_items_search_vectors_batch,
    search_items,
)
from smartrss.security.safe_http import FetchResult
from smartrss.sync.schemas import (
    FeedSetFieldMutation,
    ItemMarkBulkReadMutation,
    SyncPushRequest,
)
from smartrss.sync.service import sync_push
from smartrss.worker.stages.expire_items import run_expire_items
from smartrss.worker.stages.poll_feed import run_poll_feed


async def test_k01_category_delete_race_never_leaves_feed_on_deleted_category(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """K-01: FOR SHARE on category check blocks concurrent DELETE until push commits."""
    client = api_env["client"]
    device_id: uuid.UUID = api_env["device_id"]
    now = datetime.now(UTC)

    cat_id = uuid.uuid4()
    feed_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(Category(id=cat_id, name="RaceCat"))
        await s.flush()
        s.add(Feed(id=feed_id, feed_url="https://k01.example.com/rss", title="K01 Feed"))
        await s.flush()
        await sync_log.record(
            s,
            [SyncEvent("category", cat_id, "upsert"), SyncEvent("feed", feed_id, "upsert")],
        )
        await s.commit()

    real_validate = sync_service_mod._validate_feed_field_value
    delete_task: asyncio.Task[Any] | None = None

    def _hooked_validate(
        field: str,
        value: Any,
        active_category_ids: set[uuid.UUID],
    ) -> tuple[bool, Any, str | None]:
        nonlocal delete_task
        if delete_task is None:
            # Fire concurrent DELETE /categories/{cat_id} after step 3a and before step 4 flush.
            # With FOR SHARE held by push, DELETE must block until push commits, then clear
            # feed.category_id cleanly instead of committing a dangling feed -> deleted category!
            delete_task = asyncio.create_task(client.delete(f"/api/v1/categories/{cat_id}"))
        return real_validate(field, value, active_category_ids)

    monkeypatch.setattr(sync_service_mod, "_validate_feed_field_value", _hooked_validate)

    # Wrap session.flush in the push session so the delete task has a chance to run on the loop
    # and hit the FOR UPDATE on Category *before* push commits.
    async with session_factory() as push_session:
        real_flush = push_session.flush

        async def _slow_flush(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(0.08)
            return await real_flush(*args, **kwargs)

        monkeypatch.setattr(push_session, "flush", _slow_flush)

        push_resp = await sync_push(
            push_session,
            device_id=device_id,
            request=SyncPushRequest(
                mutations=[
                    FeedSetFieldMutation(
                        client_mutation_id=uuid.uuid4(),
                        client_timestamp=now + timedelta(seconds=30),
                        type="feed_set_field",
                        feed_id=feed_id,
                        field="category_id",
                        value=str(cat_id),
                    )
                ]
            ),
        )
        assert push_resp.results[0].outcome in ("applied", "rejected")

    assert delete_task is not None
    del_resp = await delete_task
    assert del_resp.status_code == 204

    # Invariant: a feed must NEVER end up pointing to a soft-deleted category!
    async with session_factory() as sv:
        cat_row = await sv.get(Category, cat_id)
        feed_row = await sv.get(Feed, feed_id)
        assert cat_row is not None and cat_row.deleted_at is not None
        assert feed_row is not None
        assert feed_row.category_id is None


async def test_k01_online_patch_and_post_feed_lock_category_for_share_before_feed(
    api_env: dict[str, Any],
    db_engine: AsyncEngine,
) -> None:
    """K-01 (online paths): POST and PATCH /feeds lock categories FOR SHARE before feeds."""
    client = api_env["client"]
    cat_id = (await client.post("/api/v1/categories", json={"name": "OnlineCat"})).json()["id"]

    captured: list[str] = []

    def _capture_locks(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        upper = statement.upper()
        if "FROM CATEGORIES" in upper and "FOR SHARE" in upper:
            captured.append("categories:FOR SHARE")
        elif "FROM FEEDS" in upper and "FOR UPDATE" in upper:
            captured.append("feeds:FOR UPDATE")

    sync_eng = db_engine.sync_engine
    event.listen(sync_eng, "before_cursor_execute", _capture_locks)
    try:
        r_post = await client.post(
            "/api/v1/feeds",
            json={
                "feed_url": "https://k01-online.example.com/rss",
                "title": "K01 Online",
                "category_id": cat_id,
            },
        )
        assert r_post.status_code == 201
        feed_id = r_post.json()["id"]
        assert captured[:2] == ["categories:FOR SHARE", "feeds:FOR UPDATE"]

        captured.clear()
        r_patch = await client.patch(f"/api/v1/feeds/{feed_id}", json={"category_id": cat_id})
        assert r_patch.status_code == 200
        assert captured[:2] == ["categories:FOR SHARE", "feeds:FOR UPDATE"]
    finally:
        event.remove(sync_eng, "before_cursor_execute", _capture_locks)


async def test_k01_f1_opml_rejects_deleted_and_nonexistent_default_category_id(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """K-01-F1: OPML import returns 422 on deleted or nonexistent default_category_id."""
    client = api_env["client"]
    del_cat_id = (await client.post("/api/v1/categories", json={"name": "DeletedOpmlCat"})).json()[
        "id"
    ]
    r_del = await client.delete(f"/api/v1/categories/{del_cat_id}")
    assert r_del.status_code == 204

    # Seed an unsubscribed feed to verify it is NOT reactivated on 422
    unsub_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(
            Feed(
                id=unsub_id,
                feed_url="https://opml-unsub.example.com/rss",
                title="Unsub",
                status="unsubscribed",
            )
        )
        await s.flush()
        await sync_log.record(s, [SyncEvent("feed", unsub_id, "upsert")])
        await s.commit()

    opml_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <opml version="2.0">
      <body>
        <outline type="rss" text="New Feed" xmlUrl="https://opml-new.example.com/rss" />
        <outline type="rss" text="Unsub" xmlUrl="https://opml-unsub.example.com/rss" />
      </body>
    </opml>"""

    # 1. Deleted default_category_id -> 422; no feed created or reactivated
    r_deleted = await client.post(
        "/api/v1/feeds/import-opml",
        json={"opml_xml": opml_xml, "default_category_id": del_cat_id},
    )
    assert r_deleted.status_code == 422
    assert r_deleted.json()["detail"] == "Category not found"

    async with session_factory() as sv:
        all_feeds = (await sv.execute(sa.select(Feed))).scalars().all()
        assert len(all_feeds) == 1
        assert all_feeds[0].id == unsub_id
        assert all_feeds[0].status == "unsubscribed"
        assert all_feeds[0].category_id is None

    # 2. Nonexistent default_category_id -> 422 (never unhandled FK error)
    r_missing = await client.post(
        "/api/v1/feeds/import-opml",
        json={"opml_xml": opml_xml, "default_category_id": str(uuid.uuid4())},
    )
    assert r_missing.status_code == 422
    assert r_missing.json()["detail"] == "Category not found"

    async with session_factory() as sv:
        all_feeds = (await sv.execute(sa.select(Feed))).scalars().all()
        assert len(all_feeds) == 1


async def test_k01_f1_opml_locks_categories_for_share_before_feed_writes(
    api_env: dict[str, Any],
    db_engine: AsyncEngine,
) -> None:
    """K-01-F1: OPML import acquires FOR SHARE on categories before writing any feeds."""
    client = api_env["client"]
    cat_id = (await client.post("/api/v1/categories", json={"name": "ActiveOpmlCat"})).json()["id"]

    opml_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <opml version="2.0">
      <body>
        <outline type="rss" text="OPML Lock Feed" xmlUrl="https://opml-lock.example.com/rss" />
      </body>
    </opml>"""

    captured: list[str] = []

    def _capture_opml_locks(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        upper = statement.lstrip().upper()
        if "FROM CATEGORIES" in upper and "FOR SHARE" in upper:
            captured.append("categories:FOR SHARE")
        elif upper.startswith(("INSERT INTO FEEDS", "UPDATE FEEDS")):
            captured.append("feeds:WRITE")

    sync_eng = db_engine.sync_engine
    event.listen(sync_eng, "before_cursor_execute", _capture_opml_locks)
    try:
        r_ok = await client.post(
            "/api/v1/feeds/import-opml",
            json={"opml_xml": opml_xml, "default_category_id": cat_id},
        )
        assert r_ok.status_code == 200
        assert r_ok.json()["imported"] == 1
    finally:
        event.remove(sync_eng, "before_cursor_execute", _capture_opml_locks)

    assert captured == ["categories:FOR SHARE", "feeds:WRITE"]


async def test_k02_poll_feed_updates_matched_existing_items_in_strict_id_asc_order(
    db_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """K-02: Capture SQL UPDATE order during poll_feed and prove items lock in strict id ASC."""
    now = datetime.now(UTC)
    feed_id = uuid.uuid4()
    low_id = uuid.UUID("00000000-0000-0000-0000-000000000011")
    high_id = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffee")

    async with session_factory() as s:
        s.add(
            Feed(
                id=feed_id,
                feed_url="https://k02.example.com/rss",
                title="K02 Feed",
                initial_backfill_done=True,
                fetch_full_text=False,
            )
        )
        await s.flush()
        s.add_all(
            [
                Item(
                    id=low_id,
                    feed_id=feed_id,
                    guid="g-low",
                    raw_url="https://k02.example.com/low",
                    canonical_url="https://k02.example.com/low",
                    title="Old Low",
                    fetched_at=now,
                    entry_hash="old-low",
                    read_state_updated_at=now,
                    starred_updated_at=now,
                ),
                Item(
                    id=high_id,
                    feed_id=feed_id,
                    guid="g-high",
                    raw_url="https://k02.example.com/high",
                    canonical_url="https://k02.example.com/high",
                    title="Old High",
                    fetched_at=now,
                    entry_hash="old-high",
                    read_state_updated_at=now,
                    starred_updated_at=now,
                ),
            ]
        )
        await s.flush()
        await sync_log.record(
            s,
            [
                SyncEvent("feed", feed_id, "upsert"),
                SyncEvent("item", low_id, "upsert"),
                SyncEvent("item", high_id, "upsert"),
            ],
        )
        await s.commit()

    # RSS feed deliberately lists high_id BEFORE low_id
    rss_bytes = make_rss_xml(
        "K02 Feed",
        [
            {
                "guid": "g-high",
                "title": "New High",
                "link": "https://k02.example.com/high",
                "summary": "<p>h</p>",
            },
            {
                "guid": "g-low",
                "title": "New Low",
                "link": "https://k02.example.com/low",
                "summary": "<p>l</p>",
            },
        ],
    )
    stub_http = StubSafeHttpClient(
        lambda url, _hdrs: FetchResult(
            status_code=200,
            final_url=url,
            headers={"content-type": "application/rss+xml"},
            body_bytes=rss_bytes,
            content_type="application/rss+xml",
            permanent_redirect_url=None,
        )
    )

    updated_item_ids: list[uuid.UUID] = []

    def _capture_update(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if (
            statement.lstrip().upper().startswith("UPDATE ITEMS SET TITLE")
            and isinstance(parameters, dict)
            and "id_1" in parameters
        ):
            updated_item_ids.append(parameters["id_1"])

    sync_eng = db_engine.sync_engine
    event.listen(sync_eng, "before_cursor_execute", _capture_update)
    try:
        outcome = await run_poll_feed(session_factory, stub_http, feed_id=feed_id)
        assert outcome.items_updated == 2
    finally:
        event.remove(sync_eng, "before_cursor_execute", _capture_update)

    assert updated_item_ids == [low_id, high_id]


async def test_k03_search_batches_headlines_and_offloads_text_prep_to_thread(
    db_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """K-03: Search issues 1 batched ts_headline query & offloads HTML prep via to_thread."""
    now = datetime.now(UTC)
    feed_id = uuid.uuid4()
    item_ids = [uuid.uuid4() for _ in range(5)]

    async with session_factory() as s:
        s.add(Feed(id=feed_id, feed_url="https://k03.example.com/rss", title="K03 Feed"))
        await s.flush()
        entries: list[SearchVectorInput] = []
        for idx, iid in enumerate(item_ids):
            summary_html = f"<p>Article {idx} discussing pgvector search performance</p>"
            s.add(
                Item(
                    id=iid,
                    feed_id=feed_id,
                    guid=f"k03-{idx}",
                    raw_url=f"https://k03.example.com/{idx}",
                    canonical_url=f"https://k03.example.com/{idx}",
                    title=f"K03 Title {idx}",
                    fetched_at=now - timedelta(minutes=idx),
                    raw_summary=summary_html,
                    read_state_updated_at=now,
                    starred_updated_at=now,
                )
            )
            entries.append(
                SearchVectorInput(
                    item_id=iid,
                    title=f"K03 Title {idx}",
                    author=None,
                    raw_summary=summary_html,
                    extracted_text=None,
                )
            )
        await s.flush()

        # Spy on asyncio.to_thread for rebuild_items_search_vectors_batch
        real_to_thread = asyncio.to_thread
        batch_thread_calls = 0

        async def _spy_batch_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal batch_thread_calls
            batch_thread_calls += 1
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", _spy_batch_to_thread)
        await rebuild_items_search_vectors_batch(s, entries)
        assert batch_thread_calls >= 1

        # Also check single-item rebuild_item_search_vector uses to_thread
        single_before = batch_thread_calls
        await rebuild_item_search_vector(
            s,
            item_id=item_ids[0],
            title="K03 Title 0",
            author=None,
            raw_summary="<p>Article 0 discussing pgvector search performance</p>",
            extracted_text=None,
        )
        assert batch_thread_calls > single_before

        await sync_log.record(
            s,
            [
                SyncEvent("feed", feed_id, "upsert"),
                *[SyncEvent("item", iid, "upsert") for iid in item_ids],
            ],
        )
        await s.commit()

    # Now spy on BOTH ts_headline SQL count and asyncio.to_thread during search_items
    headline_queries = 0

    def _count_headlines(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        nonlocal headline_queries
        if "ts_headline(" in statement:
            headline_queries += 1

    search_thread_calls = 0

    async def _spy_search_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal search_thread_calls
        search_thread_calls += 1
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _spy_search_to_thread)
    sync_eng = db_engine.sync_engine
    event.listen(sync_eng, "before_cursor_execute", _count_headlines)
    try:
        async with session_factory() as s:
            page = await search_items(s, q="pgvector", limit=10)
    finally:
        event.remove(sync_eng, "before_cursor_execute", _count_headlines)

    assert page.total == 5
    assert len(page.results) == 5
    assert all(len(r.snippet) >= 1 for r in page.results)
    assert headline_queries == 1  # Single batched ts_headline query for all 5 rows!
    assert search_thread_calls >= 2  # Text prep + headline fragment parsing offloaded!


async def test_k04_scoped_bulk_read_update_restricted_to_prelocked_candidates(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """K-04: Scoped bulk mark-read UPDATE never touches late-arriving rows outside step 3c."""
    device_id: uuid.UUID = api_env["device_id"]
    now = datetime.now(UTC)

    feed_id = uuid.uuid4()
    item_a = uuid.uuid4()  # initially unread (pre-locked candidate in step 3c)
    item_b = uuid.uuid4()  # initially read (NOT in step 3c candidate set)

    async with session_factory() as s:
        s.add(Feed(id=feed_id, feed_url="https://k04.example.com/rss", title="K04 Feed"))
        await s.flush()
        s.add_all(
            [
                Item(
                    id=item_a,
                    feed_id=feed_id,
                    guid="k04-a",
                    raw_url="https://k04.example.com/a",
                    canonical_url="https://k04.example.com/a",
                    title="Item A",
                    fetched_at=now - timedelta(minutes=5),
                    state="unread",
                    read_state_updated_at=now - timedelta(minutes=5),
                    starred_updated_at=now - timedelta(minutes=5),
                    ingest_sync_seq=1,
                ),
                Item(
                    id=item_b,
                    feed_id=feed_id,
                    guid="k04-b",
                    raw_url="https://k04.example.com/b",
                    canonical_url="https://k04.example.com/b",
                    title="Item B",
                    fetched_at=now - timedelta(minutes=5),
                    state="read",
                    read_at=now - timedelta(minutes=4),
                    read_state_updated_at=now - timedelta(minutes=4),
                    starred_updated_at=now - timedelta(minutes=5),
                    ingest_sync_seq=2,
                ),
            ]
        )
        await s.flush()
        await sync_log.record(
            s,
            [
                SyncEvent("feed", feed_id, "upsert"),
                SyncEvent("item", item_a, "upsert"),
                SyncEvent("item", item_b, "upsert"),
            ],
        )
        await s.commit()
        epoch = (await s.get(SyncState, 1)).instance_epoch  # type: ignore[union-attr]

    real_apply_bulk = sync_service_mod._apply_bulk_mark_read

    async def _hooked_apply_bulk(*args: Any, **kwargs: Any) -> Any:
        # After step 3c has locked candidates, flip item_b read -> unread on a 2nd connection!
        async with session_factory() as s2:
            b_row = await s2.get(Item, item_b)
            assert b_row is not None
            b_row.state = "unread"
            b_row.read_at = None
            b_row.read_state_updated_at = now - timedelta(seconds=30)
            await s2.flush()
            await sync_log.record(s2, [SyncEvent("item", item_b, "upsert")])
            await s2.commit()
        return await real_apply_bulk(*args, **kwargs)

    monkeypatch.setattr(sync_service_mod, "_apply_bulk_mark_read", _hooked_apply_bulk)

    async with session_factory() as sp:
        resp = await sync_push(
            sp,
            device_id=device_id,
            request=SyncPushRequest(
                mutations=[
                    ItemMarkBulkReadMutation(
                        client_mutation_id=uuid.uuid4(),
                        client_timestamp=now + timedelta(seconds=10),
                        type="item_mark_bulk_read",
                        scope="feed",
                        feed_id=feed_id,
                        up_to_sync_seq=100,
                        epoch=epoch,
                    )
                ]
            ),
        )

    assert resp.results[0].outcome == "applied"
    assert resp.results[0].affected_count == 1  # Only pre-locked item_a!

    async with session_factory() as sv:
        assert (await sv.get(Item, item_a)).state == "read"  # type: ignore[union-attr]
        assert (await sv.get(Item, item_b)).state == "unread"  # type: ignore[union-attr]


async def test_k07_expire_items_uses_db_clock_not_python_clock(
    db_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """K-07: expire_items UPDATE uses PostgreSQL now() for expired_at/read_state_updated_at."""
    old_ts = datetime.now(UTC) - timedelta(days=45)
    feed_id = uuid.uuid4()
    item_id = uuid.uuid4()

    with bypass_sync_guard():
        async with session_factory() as s:
            s.add(Feed(id=feed_id, feed_url="https://k07.example.com/rss", title="K07 Feed"))
            await s.flush()
            s.add(
                Item(
                    id=item_id,
                    feed_id=feed_id,
                    guid="k07-1",
                    raw_url="https://k07.example.com/1",
                    canonical_url="https://k07.example.com/1",
                    title="Old Unread",
                    fetched_at=old_ts,
                    state="unread",
                    is_starred=False,
                    read_state_updated_at=old_ts,
                    starred_updated_at=old_ts,
                )
            )
            await s.commit()

    captured_update_sql: list[str] = []

    def _capture_expire(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("UPDATE ITEMS SET STATE"):
            captured_update_sql.append(statement)

    sync_eng = db_engine.sync_engine
    event.listen(sync_eng, "before_cursor_execute", _capture_expire)
    try:
        expired = await run_expire_items(session_factory)
        assert expired == 1
    finally:
        event.remove(sync_eng, "before_cursor_execute", _capture_expire)

    assert len(captured_update_sql) == 1
    sql_lower = captured_update_sql[0].lower()
    assert "expired_at=now()" in sql_lower
    assert "read_state_updated_at=now()" in sql_lower
    assert "updated_at=now()" in sql_lower
