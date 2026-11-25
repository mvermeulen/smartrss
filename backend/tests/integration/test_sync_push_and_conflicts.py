"""Integration tests for sync/push LWW, idempotency, retries, bulk mark-read, and benchmarks."""

from __future__ import annotations

import asyncio
import itertools
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import StubSafeHttpClient, make_rss_xml

from smartrss.db.models import Feed, Item, Job, SyncChange, SyncMutation, SyncState
from smartrss.db.sync_log import SyncEvent, bypass_sync_guard, sync_log
from smartrss.security.auth import create_pairing_code, redeem_pairing_code
from smartrss.security.safe_http import FetchResult
from smartrss.sync.schemas import (
    ItemMarkBulkReadMutation,
    ItemSetStarredMutation,
    ItemSetStateMutation,
    SyncMutationInput,
    SyncPushRequest,
)
from smartrss.sync.service import sync_push
from smartrss.worker.stages.expire_items import run_expire_items
from smartrss.worker.stages.maintenance import run_retention_cleanup
from smartrss.worker.stages.poll_feed import run_poll_feed


async def _seed_feed_and_items(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    count: int = 3,
    state: str = "unread",
    now: datetime | None = None,
    raw_summary: str | None = "<p>summary</p>",
    ingest_seq_start: int = 1,
) -> tuple[uuid.UUID, list[uuid.UUID], uuid.UUID]:
    ts = now or datetime.now(UTC)
    feed_id = uuid.uuid4()
    item_ids = [uuid.uuid4() for _ in range(count)]
    async with session_factory() as s:
        s.add(Feed(id=feed_id, feed_url=f"https://push-{feed_id}.example.com/rss", title="Push"))
        await s.flush()
        for idx, iid in enumerate(item_ids):
            s.add(
                Item(
                    id=iid,
                    feed_id=feed_id,
                    guid=f"g-{idx}",
                    raw_url=f"https://push.example.com/{iid}",
                    canonical_url=f"https://push.example.com/{iid}",
                    title=f"Item {idx}",
                    fetched_at=ts,
                    raw_summary=raw_summary,
                    state=state,
                    read_at=ts if state == "read" else None,
                    expired_at=ts if state == "expired" else None,
                    read_state_updated_at=ts,
                    starred_updated_at=ts,
                    ingest_sync_seq=ingest_seq_start + idx,
                )
            )
        await s.flush()
        await sync_log.record(
            s,
            [
                SyncEvent("feed", feed_id, "upsert"),
                *[SyncEvent("item", iid, "upsert") for iid in item_ids],
            ],
        )
        await s.commit()
        epoch = (await s.get(SyncState, 1)).instance_epoch  # type: ignore[union-attr]
    return feed_id, item_ids, epoch


async def test_1b_t02b_real_postgres_hypothesis_mixed_batch_permutations(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Real-PG hypothesis test permuting mixed batches including scope='items' bulk read (P-01)."""
    device_id: uuid.UUID = api_env["device_id"]
    base_now = datetime.now(UTC)

    # Generate small mixed batches via hypothesis and test permutations against real PostgreSQL
    samples: list[list[tuple[str, int, Any]]] = []

    @settings(max_examples=6, deadline=None)
    @given(
        ops=st.lists(
            st.one_of(
                st.tuples(
                    st.just("state"),
                    st.integers(min_value=1, max_value=4),
                    st.sampled_from(["read", "unread"]),
                ),
                st.tuples(
                    st.just("star"),
                    st.integers(min_value=1, max_value=4),
                    st.booleans(),
                ),
                st.tuples(
                    st.just("bulk_items"),
                    st.integers(min_value=1, max_value=4),
                    st.just("read"),
                ),
            ),
            min_size=2,
            max_size=4,
        )
    )
    def _collect(ops: list[tuple[str, int, Any]]) -> None:
        samples.append(ops)

    _collect()

    for ops in samples:
        outcomes_by_perm: list[tuple[str, bool, datetime, datetime]] = []
        perms = list(itertools.islice(itertools.permutations(ops), 4))
        for perm in perms:
            _, item_ids, _ = await _seed_feed_and_items(
                session_factory, count=1, state="unread", now=base_now - timedelta(minutes=1)
            )
            iid = item_ids[0]
            muts: list[SyncMutationInput] = []
            for kind, sec, val in perm:
                ts = base_now + timedelta(seconds=sec)
                if kind == "state":
                    muts.append(
                        ItemSetStateMutation(
                            client_mutation_id=uuid.uuid4(),
                            client_timestamp=ts,
                            type="item_set_state",
                            item_id=iid,
                            state=val,
                        )
                    )
                elif kind == "star":
                    muts.append(
                        ItemSetStarredMutation(
                            client_mutation_id=uuid.uuid4(),
                            client_timestamp=ts,
                            type="item_set_starred",
                            item_id=iid,
                            is_starred=bool(val),
                        )
                    )
                else:
                    muts.append(
                        ItemMarkBulkReadMutation(
                            client_mutation_id=uuid.uuid4(),
                            client_timestamp=ts,
                            type="item_mark_bulk_read",
                            scope="items",
                            item_ids=[iid],
                        )
                    )
            async with session_factory() as sp:
                await sync_push(sp, device_id=device_id, request=SyncPushRequest(mutations=muts))
            async with session_factory() as sv:
                row = await sv.get(Item, iid)
                assert row is not None
                outcomes_by_perm.append(
                    (row.state, row.is_starred, row.read_state_updated_at, row.starred_updated_at)
                )
        for out in outcomes_by_perm[1:]:
            assert out == outcomes_by_perm[0]


async def test_1b_t05_push_idempotency_payload_hash_concurrent_retry_and_expired_transitions(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify replay, payload_sha256 conflict, concurrent duplicate push, caps, & expired rules."""
    client = api_env["client"]
    now = datetime.now(UTC)

    feed_id, item_ids, _epoch = await _seed_feed_and_items(
        session_factory, count=2, state="unread", now=now - timedelta(minutes=5)
    )
    item_1, item_2 = item_ids
    # Make item_2 expired (aged 40 days)
    with bypass_sync_guard():
        async with session_factory() as s:
            it2 = await s.get(Item, item_2)
            assert it2 is not None
            it2.state = "expired"
            it2.fetched_at = now - timedelta(days=40)
            it2.expired_at = now - timedelta(days=10)
            it2.read_state_updated_at = now - timedelta(days=10)
            await s.commit()

    # Empty push succeeds cleanly
    r_empty = await client.post("/api/v1/sync/push", json={"mutations": []})
    assert r_empty.status_code == 200
    assert r_empty.json()["results"] == []

    # In-batch duplicate (same payload -> replay; different payload -> mutation_id_conflict)
    mid_dup = str(uuid.uuid4())
    mid_conflict_in_batch = str(uuid.uuid4())
    mid_reserved = str(uuid.uuid4())
    mid_clock_skew = str(uuid.uuid4())
    mid_missing = str(uuid.uuid4())
    mid_star_expired = str(uuid.uuid4())
    mid_restore_expired = str(uuid.uuid4())
    t_mut = (now + timedelta(seconds=5)).isoformat()

    r1 = await client.post(
        "/api/v1/sync/push",
        json={
            "mutations": [
                {
                    "client_mutation_id": mid_dup,
                    "client_timestamp": t_mut,
                    "type": "item_set_state",
                    "item_id": str(item_1),
                    "state": "read",
                },
                {
                    "client_mutation_id": mid_dup,
                    "client_timestamp": t_mut,
                    "type": "item_set_state",
                    "item_id": str(item_1),
                    "state": "read",
                },
                {
                    "client_mutation_id": mid_conflict_in_batch,
                    "client_timestamp": t_mut,
                    "type": "item_set_starred",
                    "item_id": str(item_1),
                    "is_starred": True,
                },
                {
                    "client_mutation_id": mid_conflict_in_batch,
                    "client_timestamp": t_mut,
                    "type": "item_set_starred",
                    "item_id": str(item_1),
                    "is_starred": False,
                },
                {
                    "client_mutation_id": mid_reserved,
                    "client_timestamp": t_mut,
                    "type": "split_cluster",
                    "item_id": str(item_1),
                },
                {
                    "client_mutation_id": mid_clock_skew,
                    "client_timestamp": "1970-01-01T00:00:00Z",
                    "type": "item_set_state",
                    "item_id": str(item_1),
                    "state": "unread",
                },
                {
                    "client_mutation_id": mid_missing,
                    "client_timestamp": t_mut,
                    "type": "item_set_state",
                    "item_id": str(uuid.uuid4()),
                    "state": "read",
                },
                {
                    "client_mutation_id": mid_star_expired,
                    "client_timestamp": (now + timedelta(seconds=1)).isoformat(),
                    "type": "item_set_starred",
                    "item_id": str(item_2),
                    "is_starred": True,
                },
            ]
        },
    )
    assert r1.status_code == 200
    res1 = r1.json()["results"]
    assert res1[0]["outcome"] == "applied"
    assert res1[1]["outcome"] == "applied"
    assert res1[2]["outcome"] == "applied"
    assert res1[3]["outcome"] == "rejected"
    assert res1[3]["reason"] == "mutation_id_conflict"
    assert res1[4]["outcome"] == "rejected"
    assert res1[4]["reason"] == "unsupported_mutation_type"
    assert res1[5]["outcome"] == "rejected"
    assert res1[5]["reason"] == "clock_skew"
    assert res1[5]["current_entity_type"] == "item"
    assert "raw_summary" not in res1[5]["current_entity"]
    assert res1[6]["outcome"] == "rejected"
    assert res1[6]["reason"] == "entity_not_found"
    assert res1[6]["current_entity"] is None
    assert res1[7]["outcome"] == "applied"

    # Starring expired item left state='expired'
    async with session_factory() as s:
        it2 = await s.get(Item, item_2)
        assert it2 is not None
        assert it2.state == "expired"
        assert it2.is_starred is True

    # Restore expired -> unread sets unexpired_at and survives expire_items even after unstar!
    r_restore = await client.post(
        "/api/v1/sync/push",
        json={
            "mutations": [
                {
                    "client_mutation_id": mid_restore_expired,
                    "client_timestamp": (now + timedelta(seconds=6)).isoformat(),
                    "type": "item_set_state",
                    "item_id": str(item_2),
                    "state": "unread",
                },
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": (now + timedelta(seconds=7)).isoformat(),
                    "type": "item_set_starred",
                    "item_id": str(item_2),
                    "is_starred": False,
                },
            ]
        },
    )
    assert r_restore.status_code == 200
    async with session_factory() as s:
        it2 = await s.get(Item, item_2)
        assert it2 is not None
        assert it2.state == "unread"
        assert it2.unexpired_at is not None
    expired_cnt = await run_expire_items(session_factory)
    assert expired_cnt == 0  # Survived expire_items thanks to unexpired_at!

    # Cross-request replay: same device + same payload -> replay; different payload -> conflict
    r_replay = await client.post(
        "/api/v1/sync/push",
        json={
            "mutations": [
                {
                    "client_mutation_id": mid_dup,
                    "client_timestamp": t_mut,
                    "type": "item_set_state",
                    "item_id": str(item_1),
                    "state": "read",
                },
                {
                    "client_mutation_id": mid_dup,
                    "client_timestamp": t_mut,
                    "type": "item_set_state",
                    "item_id": str(item_1),
                    "state": "unread",
                },
            ]
        },
    )
    assert r_replay.status_code == 200
    assert r_replay.json()["results"][0]["outcome"] == "applied"
    assert r_replay.json()["results"][1]["outcome"] == "rejected"
    assert r_replay.json()["results"][1]["reason"] == "mutation_id_conflict"

    # Cross-device replay of same client_mutation_id -> rejected: mutation_id_conflict
    async with session_factory() as s:
        c2 = await create_pairing_code(s, device_name="Dev2", platform="android")
        p2 = await redeem_pairing_code(s, raw_code=c2.code)
        await s.commit()
        tok2 = p2.bearer_token

    r_other = await client.post(
        "/api/v1/sync/push",
        headers={"Authorization": f"Bearer {tok2}"},
        json={
            "mutations": [
                {
                    "client_mutation_id": mid_dup,
                    "client_timestamp": t_mut,
                    "type": "item_set_state",
                    "item_id": str(item_1),
                    "state": "read",
                }
            ]
        },
    )
    assert r_other.status_code == 200
    assert r_other.json()["results"][0]["outcome"] == "rejected"
    assert r_other.json()["results"][0]["reason"] == "mutation_id_conflict"

    # Concurrent duplicate 50-mutation push from the same device (P-05 retry on 23505)
    _, batch_items, _ = await _seed_feed_and_items(
        session_factory, count=50, state="unread", now=now - timedelta(minutes=2)
    )
    batch_payload = {
        "mutations": [
            {
                "client_mutation_id": str(uuid.uuid4()),
                "client_timestamp": (now + timedelta(seconds=10)).isoformat(),
                "type": "item_set_state",
                "item_id": str(iid),
                "state": "read",
            }
            for iid in batch_items
        ]
    }
    resp_a, resp_b = await asyncio.gather(
        client.post("/api/v1/sync/push", json=batch_payload),
        client.post("/api/v1/sync/push", json=batch_payload),
    )
    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    assert [r["outcome"] for r in resp_a.json()["results"]] == ["applied"] * 50
    assert [r["outcome"] for r in resp_b.json()["results"]] == ["applied"] * 50

    # Cap checks (P-09): >5,000 total explicit item_ids -> 422; >2 MiB body -> 413
    too_many_ids = {
        "mutations": [
            {
                "client_mutation_id": str(uuid.uuid4()),
                "client_timestamp": t_mut,
                "type": "item_mark_bulk_read",
                "scope": "items",
                "item_ids": [str(uuid.uuid4()) for _ in range(1000)],
            }
            for _ in range(6)
        ]
    }
    r_cap = await client.post("/api/v1/sync/push", json=too_many_ids)
    assert r_cap.status_code == 422

    huge_body = b'{"mutations": [], "pad": "' + (b"x" * (2 * 1024 * 1024 + 100)) + b'"}'
    r_413 = await client.post(
        "/api/v1/sync/push",
        content=huge_body,
        headers={"Content-Type": "application/json"},
    )
    assert r_413.status_code == 413

    # Invalid feed_set_field value returns rejected + slim current_entity without aborting batch
    r_bad_field = await client.post(
        "/api/v1/sync/push",
        json={
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_mut,
                    "type": "feed_set_field",
                    "feed_id": str(feed_id),
                    "field": "status",
                    "value": "bogus_status",
                },
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_mut,
                    "type": "feed_set_field",
                    "feed_id": str(feed_id),
                    "field": "category_id",
                    "value": str(uuid.uuid4()),
                },
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_mut,
                    "type": "feed_set_field",
                    "feed_id": str(feed_id),
                    "field": "expiration_days_override",
                    "value": 99999,
                },
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_mut,
                    "type": "feed_set_field",
                    "feed_id": str(feed_id),
                    "field": "research_policy.enabled",
                    "value": "not-a-bool",
                },
            ]
        },
    )
    assert r_bad_field.status_code == 200
    br = r_bad_field.json()["results"]
    assert [x["reason"] for x in br] == [
        "invalid_field_value",
        "category_not_found",
        "invalid_field_value",
        "invalid_field_value",
    ]
    assert all(x["current_entity_type"] == "feed" for x in br)


async def test_1b_t06_bounded_mark_bulk_read_and_two_order_regression(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify ingest_sync_seq bounding, scopes, pruning survival, and P-01 two-order behavior."""
    client = api_env["client"]

    # Create category + feed and poll batch 1 & batch 2 via real poll_feed
    cat_r = await client.post("/api/v1/categories", json={"name": "BulkCat"})
    cat_id = uuid.UUID(cat_r.json()["id"])
    feed_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(
            Feed(
                id=feed_id,
                category_id=cat_id,
                feed_url="https://bulk.example.com/rss",
                title="Bulk Feed",
                initial_backfill_done=True,
                fetch_full_text=False,
            )
        )
        await s.flush()
        await sync_log.record(s, [SyncEvent("feed", feed_id, "upsert")])
        await s.commit()

    rss_b1 = make_rss_xml(
        "Bulk Feed",
        [
            {"guid": "b1-1", "title": "B1 One", "link": "https://bulk.example.com/1"},
            {"guid": "b1-2", "title": "B1 Two", "link": "https://bulk.example.com/2"},
        ],
    )
    stub1 = StubSafeHttpClient(
        lambda url, _h: FetchResult(
            status_code=200,
            final_url=url,
            headers={"content-type": "application/rss+xml"},
            body_bytes=rss_b1,
            content_type="application/rss+xml",
            permanent_redirect_url=None,
        )
    )
    await run_poll_feed(session_factory, stub1, feed_id=feed_id)

    async with session_factory() as s:
        st = await s.get(SyncState, 1)
        assert st is not None
        watermark_b1 = st.current_seq
        epoch = st.instance_epoch
        b1_items = (await s.execute(sa.select(Item).where(Item.feed_id == feed_id))).scalars().all()
        assert len(b1_items) == 2
        assert all(0 < it.ingest_sync_seq <= watermark_b1 for it in b1_items)
        # Star one batch-1 item to verify starred unread -> read stays starred
        b1_starred_id = b1_items[0].id
        b1_items[0].is_starred = True
        b1_items[0].starred_at = datetime.now(UTC)
        await s.flush()
        await sync_log.record(s, [SyncEvent("item", b1_starred_id, "upsert")])
        await s.commit()

    # Poll batch 2 (ingest_sync_seq > watermark_b1)
    rss_b2 = make_rss_xml(
        "Bulk Feed",
        [
            {"guid": "b1-1", "title": "B1 One", "link": "https://bulk.example.com/1"},
            {"guid": "b1-2", "title": "B1 Two", "link": "https://bulk.example.com/2"},
            {"guid": "b2-1", "title": "B2 New", "link": "https://bulk.example.com/3"},
        ],
    )
    stub2 = StubSafeHttpClient(
        lambda url, _h: FetchResult(
            status_code=200,
            final_url=url,
            headers={"content-type": "application/rss+xml"},
            body_bytes=rss_b2,
            content_type="application/rss+xml",
            permanent_redirect_url=None,
        )
    )
    await run_poll_feed(session_factory, stub2, feed_id=feed_id)

    # Prune ALL sync_changes to prove ingest_sync_seq survives 90-day sync_changes pruning!
    with bypass_sync_guard():
        async with session_factory() as s:
            await s.execute(
                sa.update(SyncChange).values(changed_at=datetime.now(UTC) - timedelta(days=95))
            )
            await run_retention_cleanup(s)
            await s.commit()

    # Push scoped bulk mark-read bounded to watermark_b1
    now = datetime.now(UTC)
    r_bulk = await client.post(
        "/api/v1/sync/push",
        json={
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": (now + timedelta(seconds=5)).isoformat(),
                    "type": "item_mark_bulk_read",
                    "scope": "feed",
                    "feed_id": str(feed_id),
                    "up_to_sync_seq": watermark_b1,
                    "epoch": str(epoch),
                }
            ]
        },
    )
    assert r_bulk.status_code == 200
    assert r_bulk.json()["results"][0]["outcome"] == "applied"
    assert r_bulk.json()["results"][0]["affected_count"] == 2

    async with session_factory() as s:
        items_by_guid = {
            it.guid: it
            for it in (await s.execute(sa.select(Item).where(Item.feed_id == feed_id)))
            .scalars()
            .all()
        }
        assert items_by_guid["b1-1"].state == "read"
        assert items_by_guid["b1-2"].state == "read"
        assert items_by_guid["b2-1"].state == "unread"  # Protected by up_to_sync_seq!
        starred_item = await s.get(Item, b1_starred_id)
        assert starred_item is not None and starred_item.is_starred is True

    # Category scope + older_than_ts + 0-match applied
    async with session_factory() as s:
        cur_seq = (await s.get(SyncState, 1)).current_seq  # type: ignore[union-attr]
    r_cat_bulk = await client.post(
        "/api/v1/sync/push",
        json={
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": (now + timedelta(seconds=6)).isoformat(),
                    "type": "item_mark_bulk_read",
                    "scope": "category",
                    "category_id": str(cat_id),
                    "up_to_sync_seq": cur_seq,
                    "epoch": str(epoch),
                    "older_than_ts": (now - timedelta(hours=1)).isoformat(),
                }
            ]
        },
    )
    assert r_cat_bulk.status_code == 200
    assert r_cat_bulk.json()["results"][0]["outcome"] == "applied"
    assert r_cat_bulk.json()["results"][0]["affected_count"] == 0

    # P-01 two-order regression:
    # Item X starts read@t0. Device B sends unread@t2, Device A sends bulk_read@t3 (t3 > t2).
    t0 = now - timedelta(minutes=10)
    t2 = now + timedelta(seconds=10)
    t3 = now + timedelta(seconds=20)

    # 1) scope='items' converges identically to 'read' in BOTH arrival orders
    final_items_states: list[str] = []
    for order in ("A_first", "B_first"):
        _, [x_id], _ = await _seed_feed_and_items(session_factory, count=1, state="read", now=t0)
        m_a = {
            "client_mutation_id": str(uuid.uuid4()),
            "client_timestamp": t3.isoformat(),
            "type": "item_mark_bulk_read",
            "scope": "items",
            "item_ids": [str(x_id)],
        }
        m_b = {
            "client_mutation_id": str(uuid.uuid4()),
            "client_timestamp": t2.isoformat(),
            "type": "item_set_state",
            "item_id": str(x_id),
            "state": "unread",
        }
        first, second = (m_a, m_b) if order == "A_first" else (m_b, m_a)
        await client.post("/api/v1/sync/push", json={"mutations": [first]})
        await client.post("/api/v1/sync/push", json={"mutations": [second]})
        async with session_factory() as s:
            final_items_states.append((await s.get(Item, x_id)).state)  # type: ignore[union-attr]
    assert final_items_states == ["read", "read"]

    # 2) scope='feed' documents the owner-accepted RR-1 unread-only carve-out
    final_feed_states: list[str] = []
    for order in ("A_first", "B_first"):
        fid_x, [x_id], ep_x = await _seed_feed_and_items(
            session_factory, count=1, state="read", now=t0, ingest_seq_start=1
        )
        m_a = {
            "client_mutation_id": str(uuid.uuid4()),
            "client_timestamp": t3.isoformat(),
            "type": "item_mark_bulk_read",
            "scope": "feed",
            "feed_id": str(fid_x),
            "up_to_sync_seq": 999999,
            "epoch": str(ep_x),
        }
        m_b = {
            "client_mutation_id": str(uuid.uuid4()),
            "client_timestamp": t2.isoformat(),
            "type": "item_set_state",
            "item_id": str(x_id),
            "state": "unread",
        }
        first, second = (m_a, m_b) if order == "A_first" else (m_b, m_a)
        await client.post("/api/v1/sync/push", json={"mutations": [first]})
        await client.post("/api/v1/sync/push", json={"mutations": [second]})
        async with session_factory() as s:
            final_feed_states.append((await s.get(Item, x_id)).state)  # type: ignore[union-attr]
    assert final_feed_states == ["unread", "read"]


async def test_1b_t07_online_lww_monotonicity_config_version_poll_interval_and_status(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify S-04, P-02 fast-laptop two-order monotonicity, P-06 poll_interval, and Q-4."""
    client = api_env["client"]

    # Create category and feed
    cat_r = await client.post("/api/v1/categories", json={"name": "Tech"})
    cat_id = cat_r.json()["id"]
    feed_r = await client.post(
        "/api/v1/feeds",
        json={"feed_url": "https://t07.example.com/rss", "title": "T07", "category_id": cat_id},
    )
    assert feed_r.status_code == 201
    feed_id = feed_r.json()["id"]
    orig_ver = feed_r.json()["config_version"]

    now = datetime.now(UTC)

    # (a) Offline feed_set_field never bumps config_version (S-04)
    r_offline = await client.post(
        "/api/v1/sync/push",
        json={
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": (now + timedelta(seconds=10)).isoformat(),
                    "type": "feed_set_field",
                    "feed_id": feed_id,
                    "field": "research_policy.fetch_pdfs",
                    "value": True,
                },
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": (now + timedelta(seconds=11)).isoformat(),
                    "type": "feed_set_field",
                    "feed_id": feed_id,
                    "field": "expiration_days_override",
                    "value": 14,
                },
            ]
        },
    )
    assert r_offline.status_code == 200
    assert all(r["outcome"] == "applied" for r in r_offline.json()["results"])

    # Online curation_instructions PATCH with original expected_config_version still succeeds!
    r_inst = await client.patch(
        f"/api/v1/feeds/{feed_id}",
        json={
            "curation_instructions": "Focus on databases",
            "expected_config_version": orig_ver,
        },
    )
    assert r_inst.status_code == 200
    assert r_inst.json()["config_version"] == orig_ver + 1

    # (b) P-02 fast-laptop regression in BOTH arrival orders:
    # Laptop pushes muted@+4m, online PATCH sets active, delayed tablet pushes paused@+2m.
    for order in ("tablet_after_patch", "tablet_before_patch"):
        fr = await client.post(
            "/api/v1/feeds",
            json={"feed_url": f"https://p02-{order}.example.com/rss", "title": f"P02 {order}"},
        )
        fid = fr.json()["id"]
        t_base = datetime.now(UTC)
        t_laptop = t_base + timedelta(minutes=4)
        t_tablet = t_base + timedelta(minutes=2)

        # 1. Laptop pushes muted@+4m
        await client.post(
            "/api/v1/sync/push",
            json={
                "mutations": [
                    {
                        "client_mutation_id": str(uuid.uuid4()),
                        "client_timestamp": t_laptop.isoformat(),
                        "type": "feed_set_field",
                        "feed_id": fid,
                        "field": "status",
                        "value": "muted",
                    }
                ]
            },
        )
        if order == "tablet_before_patch":
            await client.post(
                "/api/v1/sync/push",
                json={
                    "mutations": [
                        {
                            "client_mutation_id": str(uuid.uuid4()),
                            "client_timestamp": t_tablet.isoformat(),
                            "type": "feed_set_field",
                            "feed_id": fid,
                            "field": "status",
                            "value": "paused",
                        }
                    ]
                },
            )
        # Online PATCH sets active -> monotonic timestamp strictly > t_laptop!
        rp = await client.patch(f"/api/v1/feeds/{fid}", json={"status": "active"})
        assert rp.status_code == 200
        ts_after_patch = datetime.fromisoformat(rp.json()["field_updated_at"]["status"])
        assert ts_after_patch > t_laptop

        if order == "tablet_after_patch":
            rt = await client.post(
                "/api/v1/sync/push",
                json={
                    "mutations": [
                        {
                            "client_mutation_id": str(uuid.uuid4()),
                            "client_timestamp": t_tablet.isoformat(),
                            "type": "feed_set_field",
                            "feed_id": fid,
                            "field": "status",
                            "value": "paused",
                        }
                    ]
                },
            )
            assert rt.json()["results"][0]["outcome"] == "superseded"

        fg = await client.get(f"/api/v1/feeds/{fid}")
        assert fg.json()["status"] == "active"

    # Monotonic DELETE /categories/{id} beats future-clamped offline category_id
    cat2 = (await client.post("/api/v1/categories", json={"name": "MonotonicCat"})).json()["id"]
    t_future_cat = datetime.now(UTC) + timedelta(minutes=3)
    await client.post(
        "/api/v1/sync/push",
        json={
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_future_cat.isoformat(),
                    "type": "feed_set_field",
                    "feed_id": feed_id,
                    "field": "category_id",
                    "value": cat2,
                }
            ]
        },
    )
    r_del_cat = await client.delete(f"/api/v1/categories/{cat2}")
    assert r_del_cat.status_code == 204
    fg_cat = (await client.get(f"/api/v1/feeds/{feed_id}")).json()
    assert fg_cat["category_id"] is None
    assert datetime.fromisoformat(fg_cat["field_updated_at"]["category_id"]) > t_future_cat

    # (c) Q-4 unified status transition side effects: online PATCH unsubscribed -> active
    # resets health/etag/backfill and enqueues poll_feed identically to offline feed_set_field
    with bypass_sync_guard():
        async with session_factory() as s:
            f_row = await s.get(Feed, uuid.UUID(feed_id))
            assert f_row is not None
            f_row.status = "unsubscribed"
            f_row.health_state = "broken"
            f_row.consecutive_errors = 4
            f_row.etag = '"stale"'
            f_row.initial_backfill_done = True
            await s.execute(sa.delete(Job).where(Job.feed_id == uuid.UUID(feed_id)))
            await s.commit()

    r_resub = await client.patch(f"/api/v1/feeds/{feed_id}", json={"status": "active"})
    assert r_resub.status_code == 200
    assert r_resub.json()["health_state"] == "healthy"
    assert r_resub.json()["consecutive_errors"] == 0
    assert r_resub.json()["initial_backfill_done"] is False
    async with session_factory() as s:
        jobs = (
            (await s.execute(sa.select(Job).where(Job.feed_id == uuid.UUID(feed_id))))
            .scalars()
            .all()
        )
        assert any(j.job_type == "poll_feed" and j.status == "queued" for j in jobs)

    # (d) P-06: PATCH poll_interval_minutes emits feed upsert visible in sync/pull,
    # and last_fetched_at/last_success_at are absent from SyncFeedDTO
    async with session_factory() as s:
        st = await s.get(SyncState, 1)
        assert st is not None
        seq_before = st.current_seq
        epoch = st.instance_epoch

    r_pi = await client.patch(f"/api/v1/feeds/{feed_id}", json={"poll_interval_minutes": 120})
    assert r_pi.status_code == 200

    r_pull = await client.get(
        "/api/v1/sync/pull", params={"since_seq": seq_before, "epoch": str(epoch)}
    )
    assert r_pull.status_code == 200
    feed_evt = next(c for c in r_pull.json()["changes"] if c["entity_id"] == feed_id)
    assert feed_evt["data"]["poll_interval_minutes"] == 120
    assert "last_fetched_at" not in feed_evt["data"]
    assert "last_success_at" not in feed_evt["data"]


async def test_1b_t08_benchmarks_500_push_response_size_and_20k_bulk_read(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """NFR-SYNC-02: 500-mutation flush <5s, 500 all-superseded <512KB, 20k bulk read <5s."""
    client = api_env["client"]
    now = datetime.now(UTC)

    # (a) 500 mixed mutations < 5.0s
    _, items_500, _ = await _seed_feed_and_items(
        session_factory,
        count=500,
        state="unread",
        now=now - timedelta(minutes=5),
        raw_summary="S" * 32_768,
    )
    muts_500 = [
        {
            "client_mutation_id": str(uuid.uuid4()),
            "client_timestamp": (now + timedelta(seconds=10)).isoformat(),
            "type": "item_set_state" if i % 2 == 0 else "item_set_starred",
            "item_id": str(iid),
            **({"state": "read"} if i % 2 == 0 else {"is_starred": True}),
        }
        for i, iid in enumerate(items_500)
    ]
    t0 = time.monotonic()
    r500 = await client.post("/api/v1/sync/push", json={"mutations": muts_500})
    elapsed_500 = time.monotonic() - t0
    assert r500.status_code == 200
    assert elapsed_500 < 5.0
    assert all(r["outcome"] == "applied" for r in r500.json()["results"])

    async with session_factory() as s:
        mut_cnt = (
            await s.execute(sa.select(sa.func.count(SyncMutation.client_mutation_id)))
        ).scalar_one()
        assert mut_cnt == 500

    # (b) 500 all-superseded mutations against 32 KB raw_summary items -> response < 512 KB (P-08)
    stale_500 = [
        {
            "client_mutation_id": str(uuid.uuid4()),
            "client_timestamp": (now - timedelta(minutes=10)).isoformat(),
            "type": "item_set_state",
            "item_id": str(iid),
            "state": "unread",
        }
        for iid in items_500
    ]
    r_sup = await client.post("/api/v1/sync/push", json={"mutations": stale_500})
    assert r_sup.status_code == 200
    assert all(r["outcome"] == "superseded" for r in r_sup.json()["results"])
    assert len(r_sup.content) < 512 * 1024

    # (c) scope='all' over 20,000 unread items completes < 5.0s (P-09)
    feed_20k = uuid.uuid4()
    with bypass_sync_guard():
        async with session_factory() as s:
            s.add(Feed(id=feed_20k, feed_url="https://20k.example.com/rss", title="20K"))
            await s.flush()
            # Insert 20,000 unread items quickly via generate_series
            await s.execute(
                sa.text(
                    """
                    INSERT INTO items (
                        id, feed_id, guid, raw_url, canonical_url, title,
                        fetched_at, state, is_starred,
                        read_state_updated_at, starred_updated_at, ingest_sync_seq
                    )
                    SELECT
                        gen_random_uuid(), :fid, 'g-' || g,
                        'https://20k.example.com/' || g, 'https://20k.example.com/' || g,
                        'Title ' || g, :ts, 'unread', FALSE, :ts, :ts, 1
                    FROM generate_series(1, 20000) AS g
                    """
                ),
                {"fid": feed_20k, "ts": now - timedelta(minutes=5)},
            )
            await s.commit()
            epoch = (await s.get(SyncState, 1)).instance_epoch  # type: ignore[union-attr]

    t1 = time.monotonic()
    r_20k = await client.post(
        "/api/v1/sync/push",
        json={
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": (now + timedelta(seconds=30)).isoformat(),
                    "type": "item_mark_bulk_read",
                    "scope": "all",
                    "up_to_sync_seq": 1000,
                    "epoch": str(epoch),
                }
            ]
        },
    )
    elapsed_20k = time.monotonic() - t1
    assert r_20k.status_code == 200
    assert r_20k.json()["results"][0]["outcome"] == "applied"
    assert r_20k.json()["results"][0]["affected_count"] >= 20000
    assert elapsed_20k < 5.0


async def test_1b_push_edge_cases_retries_replay_snapshots_and_schema_validation(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    """Cover push transient retries (40P01->200, exhausted->503), replays, & validation edges."""
    from sqlalchemy.exc import OperationalError

    import smartrss.sync.service as sync_service_mod

    client = api_env["client"]
    now = datetime.now(UTC)
    feed_id, [item_id], epoch = await _seed_feed_and_items(
        session_factory, count=1, state="unread", now=now - timedelta(minutes=5)
    )

    # 1. Offline feed_set_field unsubscribed -> active enqueues poll_feed; missing feed rejected;
    #    invalid UUID category_id rejected; clearing category_id=None succeeds; replay of superseded
    #    item & feed returns fresh slim current_entity snapshots.
    with bypass_sync_guard():
        async with session_factory() as s:
            f = await s.get(Feed, feed_id)
            assert f is not None
            f.status = "unsubscribed"
            f.health_state = "broken"
            f.consecutive_errors = 3
            await s.commit()

    mid_sup_item = str(uuid.uuid4())
    mid_sup_feed = str(uuid.uuid4())
    t_old = (now - timedelta(hours=1)).isoformat()
    t_new = (now + timedelta(seconds=10)).isoformat()

    r_edges = await client.post(
        "/api/v1/sync/push",
        json={
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_new,
                    "type": "feed_set_field",
                    "feed_id": str(feed_id),
                    "field": "status",
                    "value": "active",
                },
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_new,
                    "type": "feed_set_field",
                    "feed_id": str(feed_id),
                    "field": "category_id",
                    "value": None,
                },
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_new,
                    "type": "feed_set_field",
                    "feed_id": str(feed_id),
                    "field": "category_id",
                    "value": "not-a-uuid",
                },
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_new,
                    "type": "feed_set_field",
                    "feed_id": str(uuid.uuid4()),
                    "field": "status",
                    "value": "muted",
                },
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_new,
                    "type": "feed_set_field",
                    "feed_id": str(feed_id),
                    "field": "research_policy.enabled",
                    "value": True,
                },
                {
                    "client_mutation_id": mid_sup_item,
                    "client_timestamp": t_old,
                    "type": "item_set_starred",
                    "item_id": str(item_id),
                    "is_starred": True,
                },
                {
                    "client_mutation_id": mid_sup_feed,
                    "client_timestamp": t_old,
                    "type": "feed_set_field",
                    "feed_id": str(feed_id),
                    "field": "status",
                    "value": "muted",
                },
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_new,
                    "type": "item_mark_bulk_read",
                    "scope": "feed",
                    "feed_id": str(uuid.uuid4()),
                    "up_to_sync_seq": 100,
                    "epoch": str(epoch),
                },
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_new,
                    "type": "item_mark_bulk_read",
                    "scope": "category",
                    "category_id": str(uuid.uuid4()),
                    "up_to_sync_seq": 100,
                    "epoch": str(epoch),
                },
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": "1970-01-01T00:00:00Z",
                    "type": "feed_set_field",
                    "feed_id": str(feed_id),
                    "field": "status",
                    "value": "paused",
                },
            ]
        },
    )
    assert r_edges.status_code == 200
    er = r_edges.json()["results"]
    assert er[0]["outcome"] == "applied"
    assert er[1]["outcome"] == "applied"
    assert er[2]["outcome"] == "rejected" and er[2]["reason"] == "invalid_field_value"
    assert er[3]["outcome"] == "rejected" and er[3]["reason"] == "entity_not_found"
    assert er[4]["outcome"] == "applied"
    assert er[5]["outcome"] == "superseded" and er[5]["current_entity_type"] == "item"
    assert er[6]["outcome"] == "superseded" and er[6]["current_entity_type"] == "feed"
    assert er[7]["outcome"] == "rejected" and er[7]["reason"] == "entity_not_found"
    assert er[8]["outcome"] == "rejected" and er[8]["reason"] == "category_not_found"
    assert er[9]["outcome"] == "rejected" and er[9]["reason"] == "clock_skew"
    assert er[9]["current_entity_type"] == "feed"

    # Replaying superseded item & feed mutations hydrates fresh slim snapshots
    r_rep = await client.post(
        "/api/v1/sync/push",
        json={
            "mutations": [
                {
                    "client_mutation_id": mid_sup_item,
                    "client_timestamp": t_old,
                    "type": "item_set_starred",
                    "item_id": str(item_id),
                    "is_starred": True,
                },
                {
                    "client_mutation_id": mid_sup_feed,
                    "client_timestamp": t_old,
                    "type": "feed_set_field",
                    "feed_id": str(feed_id),
                    "field": "status",
                    "value": "muted",
                },
            ]
        },
    )
    assert r_rep.status_code == 200
    rr = r_rep.json()["results"]
    assert rr[0]["outcome"] == "superseded" and rr[0]["current_entity_type"] == "item"
    assert rr[1]["outcome"] == "superseded" and rr[1]["current_entity_type"] == "feed"

    # Verify offline unsubscribed -> active enqueued a poll_feed job
    async with session_factory() as s:
        jobs = (await s.execute(sa.select(Job).where(Job.feed_id == feed_id))).scalars().all()
        assert any(j.job_type == "poll_feed" for j in jobs)

    # 2. Schema validation 422 edges
    for bad_payload in (
        {
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": "2026-11-25T12:00:00",
                    "type": "item_set_state",
                    "item_id": str(item_id),
                    "state": "read",
                }
            ]
        },
        {
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_new,
                    "type": "item_mark_bulk_read",
                    "scope": "all",
                    "up_to_sync_seq": 1,
                }
            ]
        },
        {
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_new,
                    "type": "item_mark_bulk_read",
                    "scope": "all",
                    "up_to_sync_seq": 1,
                    "epoch": str(epoch),
                    "feed_id": str(feed_id),
                }
            ]
        },
        {
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_new,
                    "type": "item_mark_bulk_read",
                    "scope": "feed",
                    "up_to_sync_seq": 1,
                    "epoch": str(epoch),
                }
            ]
        },
        {
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_new,
                    "type": "item_mark_bulk_read",
                    "scope": "feed",
                    "feed_id": str(feed_id),
                    "up_to_sync_seq": 1,
                    "epoch": str(epoch),
                    "item_ids": [str(item_id)],
                }
            ]
        },
        {
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_new,
                    "type": "item_mark_bulk_read",
                    "scope": "category",
                    "up_to_sync_seq": 1,
                    "epoch": str(epoch),
                }
            ]
        },
        {
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_new,
                    "type": "item_mark_bulk_read",
                    "scope": "category",
                    "category_id": str(uuid.uuid4()),
                    "up_to_sync_seq": 1,
                    "epoch": str(epoch),
                    "feed_id": str(feed_id),
                }
            ]
        },
        {
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_new,
                    "type": "item_mark_bulk_read",
                    "scope": "items",
                    "item_ids": [],
                }
            ]
        },
        {
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_new,
                    "type": "item_mark_bulk_read",
                    "scope": "items",
                    "item_ids": [str(item_id)],
                    "feed_id": str(feed_id),
                }
            ]
        },
        {
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_new,
                    "type": "item_mark_bulk_read",
                    "scope": "items",
                    "item_ids": [str(item_id)],
                    "older_than_ts": "2026-11-25T12:00:00",
                }
            ]
        },
    ):
        r_bad = await client.post("/api/v1/sync/push", json=bad_payload)
        assert r_bad.status_code == 422

    # 3. Transient retry behavior (P-05): 1st attempt raises 40P01 -> 2nd succeeds; 3x 40P01 -> 503
    class _FakePgDeadlock(Exception):
        sqlstate = "40P01"

    real_exec = sync_service_mod._execute_push_once
    attempts_seen = 0

    async def _fail_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts_seen
        attempts_seen += 1
        if attempts_seen == 1:
            raise OperationalError("stmt", {}, _FakePgDeadlock("deadlock"))
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(sync_service_mod, "_execute_push_once", _fail_once)
    r_retry_ok = await client.post("/api/v1/sync/push", json={"mutations": []})
    assert r_retry_ok.status_code == 200
    assert attempts_seen == 2

    async def _always_deadlock(*args: Any, **kwargs: Any) -> Any:
        raise OperationalError("stmt", {}, _FakePgDeadlock("deadlock"))

    monkeypatch.setattr(sync_service_mod, "_execute_push_once", _always_deadlock)
    r_503 = await client.post("/api/v1/sync/push", json={"mutations": []})
    assert r_503.status_code == 503
    assert r_503.headers.get("retry-after") == "1"
