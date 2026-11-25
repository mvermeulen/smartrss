"""Integration tests for bootstrap, pull coalescing, settings API, retention, and 0002 migration."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from alembic import command as alembic_command
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.conftest import make_alembic_config

from smartrss.db.models import (
    AppSettings,
    Category,
    Feed,
    Item,
    SyncChange,
    SyncMutation,
    SyncState,
)
from smartrss.db.sync_log import SyncEvent, bypass_sync_guard, sync_log
from smartrss.domain.sync_merge import LEAD_EXCERPT_CHARS, MAX_RAW_SUMMARY_CHARS
from smartrss.security.auth import create_pairing_code, redeem_pairing_code
from smartrss.worker.stages.maintenance import run_retention_cleanup


async def test_1b_t03_bootstrap_pagination_working_set_and_mid_bootstrap_writes(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify working-set filter, keyset pagination, bounded projections, and trailing pull."""
    client = api_env["client"]
    now = datetime.now(UTC)

    cat_id = uuid.uuid4()
    del_cat_id = uuid.uuid4()
    feed_id = uuid.uuid4()
    unread_ids = [uuid.uuid4() for _ in range(4)]
    starred_expired_id = uuid.uuid4()
    recent_read_id = uuid.uuid4()
    old_read_id = uuid.uuid4()
    unstarred_expired_id = uuid.uuid4()

    async with session_factory() as s:
        s.add_all(
            [
                Category(id=cat_id, name="Tech", sort_order=1),
                Category(
                    id=del_cat_id,
                    name="Deleted",
                    sort_order=2,
                    deleted_at=now - timedelta(hours=1),
                ),
            ]
        )
        await s.flush()
        s.add(
            Feed(
                id=feed_id,
                category_id=cat_id,
                feed_url="https://boot.example.com/rss",
                title="Boot Feed",
                last_fetched_at=now,
                last_success_at=now,
            )
        )
        await s.flush()

        items_to_add = [
            Item(
                id=uid,
                feed_id=feed_id,
                guid=f"u-{i}",
                raw_url=f"https://boot.example.com/u/{i}",
                canonical_url=f"https://boot.example.com/u/{i}",
                title=f"Unread {i}",
                fetched_at=now,
                raw_summary="x" * (MAX_RAW_SUMMARY_CHARS + 50) if i == 0 else "<p>Short</p>",
                extracted_text=("word " * 3000) if i == 0 else None,
                state="unread",
                read_state_updated_at=now,
                starred_updated_at=now,
            )
            for i, uid in enumerate(unread_ids)
        ]
        items_to_add.extend(
            [
                Item(
                    id=starred_expired_id,
                    feed_id=feed_id,
                    guid="se",
                    raw_url="https://boot.example.com/se",
                    canonical_url="https://boot.example.com/se",
                    title="Starred Expired",
                    fetched_at=now - timedelta(days=40),
                    state="expired",
                    is_starred=True,
                    expired_at=now - timedelta(days=10),
                    starred_at=now - timedelta(days=5),
                    read_state_updated_at=now - timedelta(days=10),
                    starred_updated_at=now - timedelta(days=5),
                ),
                Item(
                    id=recent_read_id,
                    feed_id=feed_id,
                    guid="rr",
                    raw_url="https://boot.example.com/rr",
                    canonical_url="https://boot.example.com/rr",
                    title="Recent Read",
                    fetched_at=now - timedelta(days=3),
                    state="read",
                    read_at=now - timedelta(days=2),
                    read_state_updated_at=now - timedelta(days=2),
                    starred_updated_at=now - timedelta(days=3),
                ),
                Item(
                    id=old_read_id,
                    feed_id=feed_id,
                    guid="or",
                    raw_url="https://boot.example.com/or",
                    canonical_url="https://boot.example.com/or",
                    title="Old Read Excluded",
                    fetched_at=now - timedelta(days=25),
                    state="read",
                    read_at=now - timedelta(days=20),
                    read_state_updated_at=now - timedelta(days=20),
                    starred_updated_at=now - timedelta(days=25),
                ),
                Item(
                    id=unstarred_expired_id,
                    feed_id=feed_id,
                    guid="ue",
                    raw_url="https://boot.example.com/ue",
                    canonical_url="https://boot.example.com/ue",
                    title="Expired Excluded",
                    fetched_at=now - timedelta(days=40),
                    state="expired",
                    expired_at=now - timedelta(days=10),
                    read_state_updated_at=now - timedelta(days=10),
                    starred_updated_at=now - timedelta(days=40),
                ),
            ]
        )
        s.add_all(items_to_add)
        await s.flush()
        events = [
            SyncEvent("category", cat_id, "upsert"),
            SyncEvent("category", del_cat_id, "delete"),
            SyncEvent("feed", feed_id, "upsert"),
            *[SyncEvent("item", it.id, "upsert") for it in items_to_add],
        ]
        await sync_log.record(s, events)
        await s.commit()

    # Page 1 (limit=3 across 6 working-set items -> 2 pages)
    r1 = await client.get("/api/v1/sync/bootstrap", params={"limit": 3})
    assert r1.status_code == 200
    p1 = r1.json()
    assert p1["watermark"] is not None
    assert p1["server_now"] is not None
    assert p1["has_more"] is True
    assert p1["next_cursor"] is not None
    assert [c["id"] for c in p1["categories"]] == [str(cat_id)]
    assert len(p1["feeds"]) == 1
    assert "last_fetched_at" not in p1["feeds"][0]
    assert "last_success_at" not in p1["feeds"][0]
    assert p1["clusters"] == []
    assert p1["interest_profile"] is None
    assert p1["app_settings"]["backfill_cap"] == 10

    page1_watermark = p1["watermark"]
    page1_epoch = p1["instance_epoch"]

    # Mid-bootstrap write: insert a new item AFTER Page 1
    mid_item_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(
            Item(
                id=mid_item_id,
                feed_id=feed_id,
                guid="mid",
                raw_url="https://boot.example.com/mid",
                canonical_url="https://boot.example.com/mid",
                title="Mid-Bootstrap Item",
                fetched_at=now,
                state="unread",
                read_state_updated_at=now,
                starred_updated_at=now,
            )
        )
        await s.flush()
        await sync_log.record(s, [SyncEvent("item", mid_item_id, "upsert")])
        await s.commit()

    # Page 2..N
    collected_items: list[dict[str, Any]] = list(p1["items"])
    cursor = p1["next_cursor"]
    while cursor is not None:
        rn = await client.get("/api/v1/sync/bootstrap", params={"cursor": cursor, "limit": 3})
        assert rn.status_code == 200
        pn = rn.json()
        assert pn["watermark"] is None
        assert pn["categories"] is None
        assert pn["feeds"] is None
        assert pn["app_settings"] is None
        assert pn["instance_epoch"] == page1_epoch
        collected_items.extend(pn["items"])
        cursor = pn["next_cursor"]

    seen_ids = [it["id"] for it in collected_items]
    assert len(seen_ids) == len(set(seen_ids))  # no duplicates
    assert str(old_read_id) not in seen_ids
    assert str(unstarred_expired_id) not in seen_ids
    for expected in (*unread_ids, starred_expired_id, recent_read_id):
        assert str(expected) in seen_ids

    # Check bounded raw_summary & lead_excerpt projection
    long_item = next(it for it in collected_items if it["id"] == str(unread_ids[0]))
    assert long_item["summary_truncated"] is True
    assert len(long_item["raw_summary"]) == MAX_RAW_SUMMARY_CHARS
    assert len(long_item["lead_excerpt"]) == LEAD_EXCERPT_CHARS

    # Trailing pull from Page 1 watermark catches the mid-bootstrap item
    rp = await client.get(
        "/api/v1/sync/pull",
        params={"since_seq": page1_watermark, "epoch": page1_epoch},
    )
    assert rp.status_code == 200
    pull_body = rp.json()
    assert pull_body["resync_required"] is False
    assert str(mid_item_id) in [c["entity_id"] for c in pull_body["changes"]]

    # Mid-pagination epoch rotation is visible on Page 2
    with bypass_sync_guard():
        async with session_factory() as s:
            st = await s.get(SyncState, 1)
            assert st is not None
            st.instance_epoch = uuid.uuid4()
            await s.commit()
    r_rot = await client.get(
        "/api/v1/sync/bootstrap", params={"cursor": p1["next_cursor"], "limit": 3}
    )
    assert r_rot.status_code == 200
    assert r_rot.json()["instance_epoch"] != page1_epoch


async def test_1b_t04_pull_coalescing_cross_entity_push_pull_and_resync_reasons(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify coalescing, [I(2), F(3)] order, two-device push->pull, tombstones, & 3 resyncs."""
    client = api_env["client"]
    now = datetime.now(UTC)

    cat_id = uuid.uuid4()
    feed_id = uuid.uuid4()
    item_id = uuid.uuid4()

    async with session_factory() as s:
        s.add(Category(id=cat_id, name="Cat", sort_order=0))
        s.add(
            Feed(
                id=feed_id,
                category_id=cat_id,
                feed_url="https://pull.example.com/rss",
                title="Pull Feed",
            )
        )
        await s.flush()
        s.add(
            Item(
                id=item_id,
                feed_id=feed_id,
                guid="g1",
                raw_url="https://pull.example.com/1",
                canonical_url="https://pull.example.com/1",
                title="Pull Item",
                fetched_at=now,
                read_state_updated_at=now,
                starred_updated_at=now,
            )
        )
        await s.flush()
        # Sequence: feed(1), item(2), feed(3), item(4), item(5), cat(6)
        seqs = await sync_log.record(
            s,
            [
                SyncEvent("feed", feed_id, "upsert"),
                SyncEvent("item", item_id, "upsert"),
                SyncEvent("feed", feed_id, "upsert"),
                SyncEvent("item", item_id, "upsert"),
                SyncEvent("item", item_id, "upsert"),
                SyncEvent("category", cat_id, "upsert"),
            ],
        )
        await s.commit()
        epoch = (await s.get(SyncState, 1)).instance_epoch  # type: ignore[union-attr]

    # (a) & (b): Coalescing page [feed(1), item(2), feed(3)] -> [item(2), feed(3)] (P-12)
    r_p12 = await client.get(
        "/api/v1/sync/pull",
        params={"since_seq": 0, "epoch": str(epoch), "limit": 3},
    )
    assert r_p12.status_code == 200
    body_p12 = r_p12.json()
    assert body_p12["next_seq"] == seqs[2]
    assert body_p12["has_more"] is True
    assert [(c["entity_type"], c["seq"]) for c in body_p12["changes"]] == [
        ("item", seqs[1]),
        ("feed", seqs[2]),
    ]

    # Soft-delete category at seq 7, then pull from seq 3 with limit=3 (before delete seq 7):
    # category upsert at seq 6 must hydrate as op="delete", data=None because deleted_at IS NOT NULL
    r_del = await client.delete(f"/api/v1/categories/{cat_id}")
    assert r_del.status_code == 204

    r_cat = await client.get(
        "/api/v1/sync/pull",
        params={"since_seq": seqs[2], "epoch": str(epoch), "limit": 3},
    )
    assert r_cat.status_code == 200
    cat_changes = r_cat.json()["changes"]
    cat_evt = next(c for c in cat_changes if c["entity_type"] == "category")
    assert cat_evt["op"] == "delete"
    assert cat_evt["data"] is None

    # Advance cursor to current watermark
    r_tail = await client.get(
        "/api/v1/sync/pull",
        params={"since_seq": r_cat.json()["next_seq"], "epoch": str(epoch)},
    )
    cursor_a = r_tail.json()["next_seq"]

    # (c) Two-device push -> pull test (P-03): Device B pushes, Device A pushes, Device A pulls
    async with session_factory() as s:
        code_b = await create_pairing_code(s, device_name="PhoneB", platform="android")
        paired_b = await redeem_pairing_code(s, raw_code=code_b.code)
        await s.commit()
        token_b = paired_b.bearer_token

    r_push_b = await client.post(
        "/api/v1/sync/push",
        headers={"Authorization": f"Bearer {token_b}"},
        json={
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": (now + timedelta(seconds=10)).isoformat(),
                    "type": "item_set_starred",
                    "item_id": str(item_id),
                    "is_starred": True,
                }
            ]
        },
    )
    assert r_push_b.status_code == 200
    assert "watermark" not in r_push_b.json()

    r_push_a = await client.post(
        "/api/v1/sync/push",
        json={
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": (now + timedelta(seconds=11)).isoformat(),
                    "type": "item_set_state",
                    "item_id": str(item_id),
                    "state": "read",
                }
            ]
        },
    )
    assert r_push_a.status_code == 200
    assert "watermark" not in r_push_a.json()

    # Device A pulls from its old cursor_a and receives BOTH B's star and A's read state
    r_pull_a = await client.get(
        "/api/v1/sync/pull",
        params={"since_seq": cursor_a, "epoch": str(epoch)},
    )
    assert r_pull_a.status_code == 200
    item_dto = next(c["data"] for c in r_pull_a.json()["changes"] if c["entity_id"] == str(item_id))
    assert item_dto["is_starred"] is True
    assert item_dto["state"] == "read"

    # Empty pull returns next_seq == since_seq
    cur_end = r_pull_a.json()["next_seq"]
    r_empty = await client.get(
        "/api/v1/sync/pull",
        params={"since_seq": cur_end, "epoch": str(epoch)},
    )
    assert r_empty.json()["next_seq"] == cur_end
    assert r_empty.json()["changes"] == []

    # (e) All 3 resync_required reasons
    r_epoch = await client.get(
        "/api/v1/sync/pull",
        params={"since_seq": cur_end, "epoch": str(uuid.uuid4())},
    )
    assert r_epoch.json()["resync_required"] is True
    assert r_epoch.json()["resync_reason"] == "epoch_mismatch"

    r_ahead = await client.get(
        "/api/v1/sync/pull",
        params={"since_seq": cur_end + 100, "epoch": str(epoch)},
    )
    assert r_ahead.json()["resync_required"] is True
    assert r_ahead.json()["resync_reason"] == "cursor_ahead"

    # Prune ALL sync_changes (empty table) -> cursor_pruned still detected via pruned_through_seq!
    with bypass_sync_guard():
        async with session_factory() as s:
            await s.execute(sa.update(SyncChange).values(changed_at=now - timedelta(days=95)))
            res = await run_retention_cleanup(s)
            await s.commit()
            assert res.sync_changes_deleted > 0
            assert res.pruned_through_seq == cur_end

    r_pruned = await client.get(
        "/api/v1/sync/pull",
        params={"since_seq": 0, "epoch": str(epoch)},
    )
    assert r_pruned.json()["resync_required"] is True
    assert r_pruned.json()["resync_reason"] == "cursor_pruned"

    # At exact pruned_through_seq boundary, pull succeeds with empty delta (not resync_required)
    r_exact = await client.get(
        "/api/v1/sync/pull",
        params={"since_seq": cur_end, "epoch": str(epoch)},
    )
    assert r_exact.json()["resync_required"] is False
    assert r_exact.json()["changes"] == []


async def test_1b_t11_online_settings_api_and_retention_cleanup(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GET/PATCH /api/v1/settings null->422, diff-gated sync, and 90d sync_mutations prune."""
    client = api_env["client"]
    device_id: uuid.UUID = api_env["device_id"]

    r_get = await client.get("/api/v1/settings")
    assert r_get.status_code == 200
    assert r_get.json()["backfill_cap"] == 10

    # Explicit null returns 422 (P-17)
    r_null = await client.patch("/api/v1/settings", json={"backfill_cap": None})
    assert r_null.status_code == 422

    # No-op patch emits zero sync_changes
    async with session_factory() as s:
        seq_before = (await s.get(SyncState, 1)).current_seq  # type: ignore[union-attr]
        epoch = (await s.get(SyncState, 1)).instance_epoch  # type: ignore[union-attr]

    r_noop = await client.patch("/api/v1/settings", json={"backfill_cap": 10})
    assert r_noop.status_code == 200
    async with session_factory() as s:
        assert (await s.get(SyncState, 1)).current_seq == seq_before  # type: ignore[union-attr]

    # Actual change emits app_settings upsert visible in sync/pull
    r_upd = await client.patch(
        "/api/v1/settings",
        json={"backfill_cap": 25, "default_expiration_days": 45},
    )
    assert r_upd.status_code == 200
    assert r_upd.json()["backfill_cap"] == 25
    assert r_upd.json()["default_expiration_days"] == 45

    r_pull = await client.get(
        "/api/v1/sync/pull", params={"since_seq": seq_before, "epoch": str(epoch)}
    )
    assert r_pull.status_code == 200
    assert any(
        c["entity_type"] == "app_settings" and c["data"]["backfill_cap"] == 25
        for c in r_pull.json()["changes"]
    )

    # Seed old + recent sync_mutations and verify retention_cleanup prunes only >90d
    now = datetime.now(UTC)
    old_mut_id = uuid.uuid4()
    new_mut_id = uuid.uuid4()
    async with session_factory() as s:
        s.add_all(
            [
                SyncMutation(
                    client_mutation_id=old_mut_id,
                    device_id=device_id,
                    mutation_type="item_set_state",
                    entity_type="item",
                    entity_id=uuid.uuid4(),
                    payload_sha256="a" * 64,
                    client_timestamp=now - timedelta(days=95),
                    effective_timestamp=now - timedelta(days=95),
                    server_applied_at=now - timedelta(days=95),
                    outcome="applied",
                ),
                SyncMutation(
                    client_mutation_id=new_mut_id,
                    device_id=device_id,
                    mutation_type="item_set_state",
                    entity_type="item",
                    entity_id=uuid.uuid4(),
                    payload_sha256="b" * 64,
                    client_timestamp=now - timedelta(days=10),
                    effective_timestamp=now - timedelta(days=10),
                    server_applied_at=now - timedelta(days=10),
                    outcome="applied",
                ),
            ]
        )
        await s.commit()

    async with session_factory() as s:
        seq_pre = (await s.get(SyncState, 1)).current_seq  # type: ignore[union-attr]
        res = await run_retention_cleanup(s)
        await s.commit()
        assert res.sync_mutations_deleted == 1
        assert await s.get(SyncMutation, old_mut_id) is None
        assert await s.get(SyncMutation, new_mut_id) is not None
        assert (await s.get(SyncState, 1)).current_seq == seq_pre  # type: ignore[union-attr]
        settings_row = await s.get(AppSettings, 1)
        assert settings_row is not None and settings_row.last_retention_cleanup_at is not None


async def test_1b_t12_alembic_0002_backfill_and_downgrade_round_trip(
    test_db_url: str,
    db_engine: AsyncEngine,
) -> None:
    """base -> 0001 -> seed HTML/<script> rows -> 0002 backfill -> downgrade 0001 -> head."""
    scratch_db = f"smartrss_1b_mig_{uuid.uuid4().hex[:8]}"
    base_url = test_db_url.rsplit("/", 1)[0]
    admin_url = f"{base_url}/postgres"
    scratch_url = f"{base_url}/{scratch_db}"

    admin_engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{scratch_db}"'))

        cfg = make_alembic_config(scratch_url)
        alembic_command.upgrade(cfg, "0001_milestone_1a_core")

        feed_id = uuid.uuid4()
        item_id = uuid.uuid4()
        sync_eng = sa.create_engine(scratch_url)
        try:
            with sync_eng.begin() as conn:
                conn.execute(
                    sa.text("INSERT INTO feeds (id, feed_url, title) VALUES (:id, :url, :title)"),
                    {"id": feed_id, "url": "https://mig.example.com/rss", "title": "Mig Feed"},
                )
                conn.execute(
                    sa.text(
                        """
                        INSERT INTO items (
                            id, feed_id, guid, raw_url, canonical_url, title,
                            fetched_at, raw_summary, extracted_text,
                            read_state_updated_at, starred_updated_at
                        ) VALUES (
                            :id, :fid, 'g1', 'https://mig.example.com/1',
                            'https://mig.example.com/1', 'Quantum Computing',
                            now(), '<p>Alpha &amp; Beta</p><script>secretjsword</script>',
                            'Detailed body text', now(), now()
                        )
                        """
                    ),
                    {"id": item_id, "fid": feed_id},
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO sync_changes (seq, entity_type, entity_id, op) "
                        "VALUES (7, 'item', :id, 'upsert')"
                    ),
                    {"id": item_id},
                )
        finally:
            sync_eng.dispose()

        # Upgrade to 0002 and verify backfills
        alembic_command.upgrade(cfg, "head")
        verify_eng = sa.create_engine(scratch_url)
        try:
            with verify_eng.connect() as conn:
                row = conn.execute(
                    sa.text(
                        """
                        SELECT ingest_sync_seq,
                               search_vector @@ websearch_to_tsquery(
                                   'english', 'quantum'
                               ) AS m_title,
                               search_vector @@ websearch_to_tsquery(
                                   'english', 'alpha'
                               ) AS m_sum,
                               search_vector @@ websearch_to_tsquery(
                                   'english', 'secretjsword'
                               ) AS m_script
                        FROM items WHERE id = :id
                        """
                    ),
                    {"id": item_id},
                ).one()
                assert row.ingest_sync_seq == 7
                assert row.m_title is True
                assert row.m_sum is True
                assert row.m_script is False  # Python HTMLParser skipped <script> (P-14)
        finally:
            verify_eng.dispose()

        # Reversible downgrade to 0001 and back to head
        alembic_command.downgrade(cfg, "0001_milestone_1a_core")
        alembic_command.upgrade(cfg, "head")
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
