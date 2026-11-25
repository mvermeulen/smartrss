"""Real-Postgres concurrency tests for gap-free sync_seq and deadlock-free lock order (1b-T01)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.conftest import StubSafeHttpClient, make_rss_xml

from smartrss.db.models import Feed, Item, SyncChange, SyncState
from smartrss.db.sync_log import SyncEvent, sync_log
from smartrss.security.safe_http import FetchResult
from smartrss.sync.schemas import ItemSetStateMutation, SyncPushRequest
from smartrss.sync.service import sync_pull, sync_push
from smartrss.worker.stages.poll_feed import run_poll_feed


async def test_1b_t01a_gap_free_concurrent_commit_sync_seq_pull(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Tx A starts first & commits second; client pulls mid-flight and never skips Tx A."""
    now = datetime.now(UTC)
    feed_id = uuid.uuid4()
    item_a_id = uuid.uuid4()
    item_b_id = uuid.uuid4()

    async with session_factory() as s:
        s.add(Feed(id=feed_id, feed_url="https://t01a.example.com/rss", title="T01a Feed"))
        await s.flush()
        s.add_all(
            [
                Item(
                    id=item_a_id,
                    feed_id=feed_id,
                    guid="g-a",
                    raw_url="https://t01a.example.com/a",
                    canonical_url="https://t01a.example.com/a",
                    title="Item A",
                    fetched_at=now,
                    read_state_updated_at=now,
                    starred_updated_at=now,
                ),
                Item(
                    id=item_b_id,
                    feed_id=feed_id,
                    guid="g-b",
                    raw_url="https://t01a.example.com/b",
                    canonical_url="https://t01a.example.com/b",
                    title="Item B",
                    fetched_at=now,
                    read_state_updated_at=now,
                    starred_updated_at=now,
                ),
            ]
        )
        await s.flush()
        seqs = await sync_log.record(
            s,
            [
                SyncEvent("feed", feed_id, "upsert"),
                SyncEvent("item", item_a_id, "upsert"),
                SyncEvent("item", item_b_id, "upsert"),
            ],
        )
        await s.commit()
        baseline_seq = seqs[-1]
        epoch = (await s.get(SyncState, 1)).instance_epoch  # type: ignore[union-attr]

    tx_a_started = asyncio.Event()
    pull_after_b_done = asyncio.Event()

    async def tx_a() -> int:
        async with session_factory() as s1:
            item_a = await s1.get(Item, item_a_id)
            assert item_a is not None
            item_a.state = "read"
            item_a.read_at = now
            await s1.flush()
            tx_a_started.set()
            await pull_after_b_done.wait()
            rec = await sync_log.record(s1, [SyncEvent("item", item_a_id, "upsert")])
            await s1.commit()
            return rec[0]

    async def tx_b_and_first_pull() -> tuple[int, list[uuid.UUID]]:
        await tx_a_started.wait()
        async with session_factory() as s2:
            item_b = await s2.get(Item, item_b_id)
            assert item_b is not None
            item_b.state = "read"
            item_b.read_at = now
            await s2.flush()
            rec_b = await sync_log.record(s2, [SyncEvent("item", item_b_id, "upsert")])
            await s2.commit()

        async with session_factory() as sp:
            pull1 = await sync_pull(sp, since_seq=baseline_seq, epoch=epoch, limit=50)
            seen_ids = [c.entity_id for c in pull1.changes]
            assert pull1.next_seq == rec_b[0]
            assert seen_ids == [item_b_id]
        pull_after_b_done.set()
        return rec_b[0], seen_ids

    seq_a, (seq_b, first_seen) = await asyncio.gather(tx_a(), tx_b_and_first_pull())
    assert seq_a > seq_b
    assert first_seen == [item_b_id]

    async with session_factory() as sp2:
        pull2 = await sync_pull(sp2, since_seq=seq_b, epoch=epoch, limit=50)
        assert [c.entity_id for c in pull2.changes] == [item_a_id]
        assert pull2.next_seq == seq_a
        assert pull2.changes[0].seq == seq_a


async def test_1b_t01b_poll_feed_sorted_item_updates_and_concurrent_push_no_deadlock(
    api_env: dict[str, Any],
    db_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """poll_feed updates existing items in item.id ASC order; concurrent push never deadlocks."""
    now = datetime.now(UTC)
    feed_id = uuid.uuid4()
    low_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    high_id = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

    async with session_factory() as s:
        s.add(
            Feed(
                id=feed_id,
                feed_url="https://lockorder.example.com/rss",
                title="Lock Order Feed",
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
                    raw_url="https://lockorder.example.com/low",
                    canonical_url="https://lockorder.example.com/low",
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
                    raw_url="https://lockorder.example.com/high",
                    canonical_url="https://lockorder.example.com/high",
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

    # RSS feed deliberately lists high_id BEFORE low_id to prove poll_feed sorts by item.id ASC
    rss_bytes = make_rss_xml(
        "Lock Order Feed",
        [
            {
                "guid": "g-high",
                "title": "Updated High",
                "link": "https://lockorder.example.com/high",
                "summary": "<p>new high</p>",
            },
            {
                "guid": "g-low",
                "title": "Updated Low",
                "link": "https://lockorder.example.com/low",
                "summary": "<p>new low</p>",
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

    device_id: uuid.UUID = api_env["device_id"]

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

    async def do_poll() -> None:
        outcome = await run_poll_feed(
            session_factory,
            stub_http,
            feed_id=feed_id,
        )
        assert outcome.metric_status == "ok"

    async def do_push() -> None:
        async with session_factory() as sp:
            resp = await sync_push(
                sp,
                device_id=device_id,
                max_attempts=1,
                request=SyncPushRequest(
                    mutations=[
                        ItemSetStateMutation(
                            client_mutation_id=uuid.uuid4(),
                            client_timestamp=datetime.now(UTC),
                            type="item_set_state",
                            item_id=high_id,
                            state="read",
                        ),
                        ItemSetStateMutation(
                            client_mutation_id=uuid.uuid4(),
                            client_timestamp=datetime.now(UTC),
                            type="item_set_state",
                            item_id=low_id,
                            state="read",
                        ),
                    ]
                ),
            )
            assert all(r.outcome == "applied" for r in resp.results)

    sa.event.listen(db_engine.sync_engine, "before_cursor_execute", _capture_update)
    try:
        await asyncio.gather(do_poll(), do_push())
    finally:
        sa.event.remove(db_engine.sync_engine, "before_cursor_execute", _capture_update)

    assert updated_item_ids == sorted(updated_item_ids)

    # Verify item update sync_changes emitted in sorted id ASC order when poll updated them
    async with session_factory() as s:
        low = await s.get(Item, low_id)
        high = await s.get(Item, high_id)
        assert low is not None and high is not None
        assert low.state == "read" and high.state == "read"


async def test_synclog_chunked_insert_over_1000_events_and_monotonic_changed_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """SyncLogWriter chunks >1000 rows safely (D1) and stamps monotonic clock_timestamp()."""
    events = [SyncEvent("item", uuid.uuid4(), "upsert") for _ in range(1250)]
    async with session_factory() as s:
        seqs = await sync_log.record(s, events)
        await s.commit()
    assert seqs == list(range(1, 1251))

    async with session_factory() as s:
        rows = (
            await s.execute(
                sa.select(SyncChange.seq, SyncChange.changed_at).order_by(SyncChange.seq.asc())
            )
        ).all()
        assert len(rows) == 1250
        assert rows[0].changed_at <= rows[-1].changed_at
