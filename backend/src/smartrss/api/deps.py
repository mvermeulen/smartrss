"""FastAPI dependencies for DB sessions, authentication, and shared security policies."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from smartrss.db.models import Device
from smartrss.security.auth import (
    GlobalSlidingWindowRateLimiter,
    authenticate_bearer_token,
)
from smartrss.security.safe_http import SSRFPolicy


def get_engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.engine
    return engine


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def get_ssrf_policy(request: Request) -> SSRFPolicy:
    policy: SSRFPolicy = request.app.state.ssrf_policy
    return policy


def get_pair_rate_limiter(request: Request) -> GlobalSlidingWindowRateLimiter:
    limiter: GlobalSlidingWindowRateLimiter = request.app.state.pair_rate_limiter
    return limiter


async def get_db_session(
    factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session


async def get_current_device(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> Device:
    auth_header = request.headers.get("authorization", "")
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    device = await authenticate_bearer_token(
        session,
        token.strip(),
        touch_session_factory=factory,
    )
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return device
