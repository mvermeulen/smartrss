"""PostgreSQL advisory-lock helpers with strict connection-pool safety (§8.2)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

logger = logging.getLogger(__name__)

LOCK_SCHEDULER = 0x5352_0001
LOCK_REAPER = 0x5352_0002
LOCK_RUN_ONCE = 0x5352_0003


async def try_advisory_xact_lock(session: AsyncSession, key: int) -> bool:
    """Acquire a transaction-scoped advisory lock (auto-released on commit/rollback)."""
    result = await session.execute(sa.text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})
    return bool(result.scalar_one())


@asynccontextmanager
async def advisory_session_lock(engine: AsyncEngine, key: int) -> AsyncIterator[bool]:
    """Acquire a session-level advisory lock on a dedicated checked-out AsyncConnection.

    The connection is never used for unrelated queries and unlocks in `finally` before
    being returned to the pool (and Postgres auto-releases it if the process crashes).
    """
    async with engine.connect() as conn:
        result = await conn.execute(sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        acquired = bool(result.scalar_one())
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    await conn.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                except Exception:
                    logger.warning(
                        "Failed to release advisory lock %s; invalidating connection", hex(key)
                    )
                    await conn.invalidate()
                except BaseException:
                    await conn.invalidate()
                    raise
