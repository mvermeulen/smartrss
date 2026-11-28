"""Batch re-canonicalization helper for items.canonical_url (E-12)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smartrss.db.models import Item
from smartrss.db.sync_log import SyncEvent, execute_synced, sync_log
from smartrss.domain.url_canon import canonicalize_url


def compute_updated_canonical_url(raw_url: str, current_canonical_url: str) -> str | None:
    """Return the recomputed canonical_url if it differs from current_canonical_url, else None."""
    try:
        recomputed = canonicalize_url(raw_url)
    except ValueError:
        try:
            recomputed = canonicalize_url(current_canonical_url)
        except ValueError:
            return None
    if recomputed == current_canonical_url:
        return None
    return recomputed


async def recanonicalize_all_items(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_size: int = 500,
) -> int:
    """Recompute items.canonical_url across all rows in id ASC batches, emitting item upserts."""
    updated_total = 0
    last_id: uuid.UUID | None = None

    while True:
        async with session_factory() as session:
            stmt = sa.select(Item.id, Item.raw_url, Item.canonical_url).order_by(Item.id.asc())
            if last_id is not None:
                stmt = stmt.where(Item.id > last_id)
            rows = (await session.execute(stmt.limit(batch_size))).all()
            if not rows:
                break

            last_id = rows[-1].id
            changed: list[tuple[uuid.UUID, str]] = []
            for row in rows:
                new_canon = compute_updated_canonical_url(row.raw_url, row.canonical_url)
                if new_canon is not None:
                    changed.append((row.id, new_canon))

            if not changed:
                continue

            now = datetime.now(UTC)
            events: list[SyncEvent] = []
            for item_id, new_canon in changed:

                def _item_event(_res: object, _iid: uuid.UUID = item_id) -> list[SyncEvent]:
                    return [SyncEvent("item", _iid, "upsert")]

                _, evs = await execute_synced(
                    session,
                    sa.update(Item)
                    .where(Item.id == item_id)
                    .values(canonical_url=new_canon, updated_at=now),
                    _item_event,
                )
                events.extend(evs)

            await sync_log.record(session, events)
            await session.commit()
            updated_total += len(changed)

    return updated_total
