"""Batched unread item expiration sweeper (expire_items, §9.3, data-model.md §6.1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smartrss.db.models import Item
from smartrss.db.sync_log import SyncEvent, execute_synced, sync_log

EXPIRE_BATCH_SIZE = 500

_SELECT_EXPIRE_IDS_SQL = sa.text(
    """
    SELECT i.id
    FROM items i
    JOIN feeds f ON f.id = i.feed_id
    CROSS JOIN app_settings s
    WHERE s.id = 1
      AND i.state = 'unread'
      AND i.is_starred = FALSE
      AND COALESCE(f.expiration_days_override, s.default_expiration_days) > 0
      AND COALESCE(i.unexpired_at, i.fetched_at)
          < now() - make_interval(
              days => COALESCE(f.expiration_days_override, s.default_expiration_days)
          )
    ORDER BY i.id
    LIMIT :batch_size
    FOR UPDATE OF i SKIP LOCKED
    """
)


async def run_expire_items(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_size: int = EXPIRE_BATCH_SIZE,
) -> int:
    """Expire unread unstarred items past effective TTL in committed batches of 500."""
    total_expired = 0

    while True:
        async with session_factory() as session:
            ids: list[uuid.UUID] = list(
                (await session.execute(_SELECT_EXPIRE_IDS_SQL, {"batch_size": batch_size}))
                .scalars()
                .all()
            )
            if not ids:
                await session.commit()
                break

            now = datetime.now(UTC)
            batch_ids = list(ids)

            def _expire_events(_res: object, _ids: list[uuid.UUID] = batch_ids) -> list[SyncEvent]:
                return [SyncEvent("item", iid, "upsert") for iid in _ids]

            _, events = await execute_synced(
                session,
                sa.update(Item)
                .where(Item.id.in_(batch_ids))
                .values(
                    state="expired",
                    expired_at=now,
                    read_state_updated_at=now,
                    updated_at=now,
                ),
                _expire_events,
            )
            await sync_log.record(session, events)
            await session.commit()

            total_expired += len(ids)
            if len(ids) < batch_size:
                break

    return total_expired
