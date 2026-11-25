"""Async SQLAlchemy engine, sessionmaker, and health check helpers."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from smartrss.db.sync_log import install_sync_guards


def build_engine(
    database_url: str,
    *,
    strict_sync_guard: bool = False,
    pool_size: int = 10,
    max_overflow: int = 10,
) -> AsyncEngine:
    """Create an AsyncEngine configured for psycopg v3."""
    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )
    if strict_sync_guard:
        install_sync_guards(engine)
    return engine


def build_sessionmaker(
    engine: AsyncEngine,
    *,
    strict_sync_guard: bool = False,
) -> async_sessionmaker[AsyncSession]:
    """Create an async_sessionmaker bound to the given engine."""
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    if strict_sync_guard:
        install_sync_guards(engine, maker)
    return maker


async def check_db_health(engine: AsyncEngine) -> bool:
    """Return True if PostgreSQL responds to SELECT 1."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
