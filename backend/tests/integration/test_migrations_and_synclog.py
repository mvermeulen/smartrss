"""Integration tests for Alembic reversibility, SyncLogWriter concurrency, and strict guards."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic import command as alembic_command
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.conftest import make_alembic_config

from smartrss.db.models import Category, Feed, SyncChange, SyncState
from smartrss.db.sync_log import (
    MissingSyncEventError,
    SyncEvent,
    bypass_sync_guard,
    execute_synced,
    sync_log,
)


async def test_alembic_upgrade_downgrade_upgrade_on_scratch_db(
    test_db_url: str,
    db_engine: AsyncEngine,
) -> None:
    """Alembic upgrade head -> downgrade base -> upgrade head runs cleanly on empty Postgres."""
    scratch_db = f"smartrss_mig_{uuid.uuid4().hex[:8]}"
    base_url = test_db_url.rsplit("/", 1)[0]
    admin_url = f"{base_url}/postgres"
    scratch_url = f"{base_url}/{scratch_db}"

    admin_engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{scratch_db}"'))

        cfg = make_alembic_config(scratch_url)
        alembic_command.upgrade(cfg, "head")
        alembic_command.downgrade(cfg, "base")
        alembic_command.upgrade(cfg, "head")

        verify_engine = sa.create_engine(scratch_url)
        try:
            with verify_engine.connect() as conn:
                seq = conn.execute(
                    sa.text("SELECT current_seq FROM sync_state WHERE id = 1")
                ).scalar_one()
                cap = conn.execute(
                    sa.text("SELECT backfill_cap FROM app_settings WHERE id = 1")
                ).scalar_one()
                assert seq == 0
                assert cap == 10
        finally:
            verify_engine.dispose()
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


async def test_synclog_concurrent_writers_monotonic_gap_free_commit_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Earlier-starting tx1 commits after tx2; sync_changes.seq stays gap-free & commit-ordered."""
    cat1_id = uuid.uuid4()
    cat2_id = uuid.uuid4()

    tx1_started = asyncio.Event()
    tx2_committed = asyncio.Event()

    async def writer_1() -> list[int]:
        async with session_factory() as s1:
            s1.add(Category(id=cat1_id, name="Tx1 First Started"))
            await s1.flush()
            tx1_started.set()
            # Wait until tx2 has fully committed before recording sync_log
            await tx2_committed.wait()
            seqs = await sync_log.record(s1, [SyncEvent("category", cat1_id, "upsert")])
            await s1.commit()
            return seqs

    async def writer_2() -> list[int]:
        await tx1_started.wait()
        async with session_factory() as s2:
            s2.add(Category(id=cat2_id, name="Tx2 Committed First"))
            await s2.flush()
            seqs = await sync_log.record(s2, [SyncEvent("category", cat2_id, "upsert")])
            await s2.commit()
            tx2_committed.set()
            return seqs

    seqs1, seqs2 = await asyncio.gather(writer_1(), writer_2())
    assert seqs2 == [1]
    assert seqs1 == [2]

    # Also test 10 concurrent writers acquiring sync_state FOR UPDATE simultaneously
    async def burst_writer(idx: int) -> int:
        cid = uuid.uuid4()
        async with session_factory() as s:
            s.add(Category(id=cid, name=f"Burst {idx}"))
            await s.flush()
            seqs = await sync_log.record(s, [SyncEvent("category", cid, "upsert")])
            await s.commit()
            return seqs[0]

    burst_seqs = await asyncio.gather(*(burst_writer(i) for i in range(10)))
    assert sorted(burst_seqs) == list(range(3, 13))

    async with session_factory() as s:
        state = await s.get(SyncState, 1)
        assert state is not None
        assert state.current_seq == 12
        all_seqs = (
            (await s.execute(sa.select(SyncChange.seq).order_by(SyncChange.seq.asc())))
            .scalars()
            .all()
        )
        assert list(all_seqs) == list(range(1, 13))


async def test_strict_sync_guards_catch_unlogged_orm_and_core_writes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Both ORM before_commit and Core before_cursor_execute guards flag unlogged synced writes."""
    # 1. ORM write without sync_log.record() raises MissingSyncEventError
    async with session_factory() as s:
        s.add(Category(id=uuid.uuid4(), name="Forgot Sync"))
        with pytest.raises(MissingSyncEventError):
            await s.commit()
        await s.rollback()

    # Seed a feed validly first
    feed_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(
            Feed(
                id=feed_id,
                feed_url="https://guard.example.com/rss",
                title="Guard Feed",
            )
        )
        await s.flush()
        await sync_log.record(s, [SyncEvent("feed", feed_id, "upsert")])
        await s.commit()

    # 2. Poll-bookkeeping-only ORM write on Feed is allowed with 0 sync events
    async with session_factory() as s:
        feed = await s.get(Feed, feed_id)
        assert feed is not None
        feed.last_fetched_at = datetime.now(UTC)
        await s.commit()

    # 3. Direct Core SQL UPDATE on feeds outside execute_synced raises MissingSyncEventError
    async with session_factory() as s:
        with pytest.raises(MissingSyncEventError):
            await s.execute(
                sa.update(Feed).where(Feed.id == feed_id).values(title="Bypassed Core Write")
            )
        await s.rollback()

    # 4. Core SQL via execute_synced returning [] (poll bookkeeping) commits cleanly
    async with session_factory() as s:
        await execute_synced(
            s,
            sa.update(Feed).where(Feed.id == feed_id).values(last_fetched_at=datetime.now(UTC)),
            lambda _res: [],
        )
        await s.commit()

    # 5. Core SQL via execute_synced returning an event but forgetting sync_log.record() raises
    async with session_factory() as s:
        await execute_synced(
            s,
            sa.update(Feed).where(Feed.id == feed_id).values(title="Forgotten Record"),
            lambda _res: [SyncEvent("feed", feed_id, "upsert")],
        )
        with pytest.raises(MissingSyncEventError):
            await s.commit()
        await s.rollback()

    # Verify bypass context manager works for test setup
    with bypass_sync_guard():
        async with session_factory() as s:
            await s.execute(sa.update(Feed).where(Feed.id == feed_id).values(etag="test-etag"))
            await s.commit()

    # AppSettings ORM guard: internal last_retention_cleanup_at needs 0 sync events;
    # client-visible backfill_cap without sync_log.record raises MissingSyncEventError
    from smartrss.db.models import AppSettings

    async with session_factory() as s:
        settings_row = await s.get(AppSettings, 1)
        assert settings_row is not None
        settings_row.last_retention_cleanup_at = datetime.now(UTC)
        await s.commit()

    async with session_factory() as s:
        settings_row = await s.get(AppSettings, 1)
        assert settings_row is not None
        settings_row.backfill_cap = 25
        with pytest.raises(MissingSyncEventError):
            await s.commit()
        await s.rollback()
