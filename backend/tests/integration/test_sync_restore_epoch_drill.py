"""End-to-end DB restore-epoch recovery drill and P-11-F1 epoch-pinning tests (1b-T09)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smartrss.db.models import Feed, Item, SyncChange, SyncMutation, SyncState
from smartrss.db.sync_log import SyncEvent, bypass_sync_guard, sync_log


async def test_1b_t09a_restore_epoch_drill_recovers_48h_journal_and_pending_outbox(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Restore rewinds DB & rotates epoch; client replays 48h journal + outbox with zero loss."""
    client = api_env["client"]
    now = datetime.now(UTC)

    feed_id = uuid.uuid4()
    item_before_backup = uuid.uuid4()
    item_after_backup_acked = uuid.uuid4()
    item_pending_offline = uuid.uuid4()

    async with session_factory() as s:
        s.add(Feed(id=feed_id, feed_url="https://restore.example.com/rss", title="Restore Feed"))
        await s.flush()
        for idx, iid in enumerate(
            (item_before_backup, item_after_backup_acked, item_pending_offline)
        ):
            s.add(
                Item(
                    id=iid,
                    feed_id=feed_id,
                    guid=f"r-{idx}",
                    raw_url=f"https://restore.example.com/{idx}",
                    canonical_url=f"https://restore.example.com/{idx}",
                    title=f"Restore Item {idx}",
                    fetched_at=now - timedelta(hours=2),
                    state="unread",
                    read_state_updated_at=now - timedelta(hours=2),
                    starred_updated_at=now - timedelta(hours=2),
                )
            )
        await s.flush()
        await sync_log.record(
            s,
            [
                SyncEvent("feed", feed_id, "upsert"),
                SyncEvent("item", item_before_backup, "upsert"),
                SyncEvent("item", item_after_backup_acked, "upsert"),
                SyncEvent("item", item_pending_offline, "upsert"),
            ],
        )
        await s.commit()

    # Client bootstraps under epoch E1
    b1 = (await client.get("/api/v1/sync/bootstrap")).json()
    epoch_e1 = b1["instance_epoch"]

    # 1) Mutation pushed BEFORE nightly backup
    mut_before_backup = {
        "client_mutation_id": str(uuid.uuid4()),
        "client_timestamp": (now - timedelta(minutes=30)).isoformat(),
        "type": "item_set_state",
        "item_id": str(item_before_backup),
        "state": "read",
    }
    r_pre = await client.post("/api/v1/sync/push", json={"mutations": [mut_before_backup]})
    assert r_pre.json()["results"][0]["outcome"] == "applied"

    async with session_factory() as s:
        backup_seq = (await s.get(SyncState, 1)).current_seq  # type: ignore[union-attr]

    # 2) Mutation pushed AFTER backup and acknowledged (kept in client's 48h replay journal)
    mut_after_backup = {
        "client_mutation_id": str(uuid.uuid4()),
        "client_timestamp": (now - timedelta(minutes=10)).isoformat(),
        "type": "item_set_starred",
        "item_id": str(item_after_backup_acked),
        "is_starred": True,
    }
    r_post = await client.post("/api/v1/sync/push", json={"mutations": [mut_after_backup]})
    assert r_post.json()["results"][0]["outcome"] == "applied"

    # Client pulls up to seq N > backup_seq
    pull_n = (
        await client.get(
            "/api/v1/sync/pull", params={"since_seq": b1["watermark"], "epoch": epoch_e1}
        )
    ).json()
    client_cursor_n = pull_n["next_seq"]
    assert client_cursor_n > backup_seq

    # Client goes offline and queues pending outbox mutation
    mut_pending = {
        "client_mutation_id": str(uuid.uuid4()),
        "client_timestamp": (now - timedelta(minutes=1)).isoformat(),
        "type": "item_set_state",
        "item_id": str(item_pending_offline),
        "state": "read",
    }

    # --- SIMULATE DB RESTORE TO backup_seq + EPOCH ROTATION E1 -> E2 ---
    epoch_e2 = uuid.uuid4()
    with bypass_sync_guard():
        async with session_factory() as s:
            # Revert item_after_backup_acked to pre-backup state and remove post-backup mutation
            it_after = await s.get(Item, item_after_backup_acked)
            assert it_after is not None
            it_after.is_starred = False
            it_after.starred_at = None
            it_after.starred_updated_at = now - timedelta(hours=2)
            await s.execute(
                sa.delete(SyncMutation).where(
                    SyncMutation.client_mutation_id
                    == uuid.UUID(mut_after_backup["client_mutation_id"])
                )
            )
            await s.execute(sa.delete(SyncChange).where(SyncChange.seq > backup_seq))
            st = await s.get(SyncState, 1)
            assert st is not None
            st.current_seq = backup_seq
            st.instance_epoch = epoch_e2
            await s.commit()

    # Client reconnects and pulls -> epoch_mismatch!
    pull_resync = (
        await client.get(
            "/api/v1/sync/pull", params={"since_seq": client_cursor_n, "epoch": epoch_e1}
        )
    ).json()
    assert pull_resync["resync_required"] is True
    assert pull_resync["resync_reason"] == "epoch_mismatch"
    assert pull_resync["instance_epoch"] == str(epoch_e2)

    # Client re-bootstraps under E2
    b2 = (await client.get("/api/v1/sync/bootstrap")).json()
    assert b2["instance_epoch"] == str(epoch_e2)

    # Client replays BOTH 48h acknowledged replay journal (mut_before_backup, mut_after_backup)
    # AND pending offline outbox (mut_pending) idempotently!
    r_flush = await client.post(
        "/api/v1/sync/push",
        json={"mutations": [mut_before_backup, mut_after_backup, mut_pending]},
    )
    assert r_flush.status_code == 200
    assert [r["outcome"] for r in r_flush.json()["results"]] == ["applied", "applied", "applied"]

    # Zero lost read/star state!
    async with session_factory() as s:
        assert (await s.get(Item, item_before_backup)).state == "read"  # type: ignore[union-attr]
        assert (await s.get(Item, item_after_backup_acked)).is_starred is True  # type: ignore[union-attr]
        assert (await s.get(Item, item_pending_offline)).state == "read"  # type: ignore[union-attr]


async def test_1b_t09b_p11_f1_old_epoch_scoped_bulk_read_rejected_protecting_post_restore_items(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """P-11-F1 regression: W=1000 -> restore 900 -> poll 901..950 -> old-epoch bulk read fails."""
    client = api_env["client"]
    now = datetime.now(UTC)
    epoch_e1 = uuid.uuid4()
    epoch_e2 = uuid.uuid4()
    feed_id = uuid.uuid4()
    pre_restore_item = uuid.uuid4()
    post_restore_items = [uuid.uuid4() for _ in range(5)]

    # Simulate post-restore state: DB rewound to 900 under E2, then poll ingested seqs 901..905
    with bypass_sync_guard():
        async with session_factory() as s:
            st = await s.get(SyncState, 1)
            assert st is not None
            st.current_seq = 905
            st.instance_epoch = epoch_e2
            s.add(Feed(id=feed_id, feed_url="https://p11f1.example.com/rss", title="P11F1"))
            await s.flush()
            s.add(
                Item(
                    id=pre_restore_item,
                    feed_id=feed_id,
                    guid="pre",
                    raw_url="https://p11f1.example.com/pre",
                    canonical_url="https://p11f1.example.com/pre",
                    title="Pre Restore Known Item",
                    fetched_at=now - timedelta(hours=3),
                    state="unread",
                    read_state_updated_at=now - timedelta(hours=3),
                    starred_updated_at=now - timedelta(hours=3),
                    ingest_sync_seq=850,
                )
            )
            for idx, iid in enumerate(post_restore_items, start=901):
                s.add(
                    Item(
                        id=iid,
                        feed_id=feed_id,
                        guid=f"post-{idx}",
                        raw_url=f"https://p11f1.example.com/{idx}",
                        canonical_url=f"https://p11f1.example.com/{idx}",
                        title=f"Unseen Post-Restore Item {idx}",
                        fetched_at=now - timedelta(minutes=55),  # fetched at 10:05
                        state="unread",
                        read_state_updated_at=now - timedelta(minutes=55),
                        starred_updated_at=now - timedelta(minutes=55),
                        ingest_sync_seq=idx,
                    )
                )
            await s.commit()

    # Offline client (last at W=1000 under E1) pushes old-epoch scoped bulk read at 11:00
    # alongside an epoch-agnostic scope='items' rewrite for pre_restore_item.
    t_action = now  # 11:00 (after 10:05 post-restore poll!)
    r_push = await client.post(
        "/api/v1/sync/push",
        json={
            "mutations": [
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_action.isoformat(),
                    "type": "item_mark_bulk_read",
                    "scope": "all",
                    "up_to_sync_seq": 1000,
                    "epoch": str(epoch_e1),
                },
                {
                    "client_mutation_id": str(uuid.uuid4()),
                    "client_timestamp": t_action.isoformat(),
                    "type": "item_mark_bulk_read",
                    "scope": "items",
                    "item_ids": [str(pre_restore_item)],
                },
            ]
        },
    )
    assert r_push.status_code == 200
    res = r_push.json()["results"]
    assert res[0]["outcome"] == "rejected"
    assert res[0]["reason"] == "epoch_mismatch"
    assert res[0]["affected_count"] is None
    assert res[1]["outcome"] == "applied"
    assert res[1]["affected_count"] == 1

    # Unseen post-restore items 901..905 remain UNREAD; pre_restore_item is READ
    async with session_factory() as s:
        assert (await s.get(Item, pre_restore_item)).state == "read"  # type: ignore[union-attr]
        for iid in post_restore_items:
            assert (await s.get(Item, iid)).state == "unread"  # type: ignore[union-attr]
