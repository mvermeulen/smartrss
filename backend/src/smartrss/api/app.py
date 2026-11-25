"""FastAPI application factory, lifespan, and security middleware wiring (§6.1)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from smartrss import __version__
from smartrss.api.routes import auth, categories, feeds, health, items, system
from smartrss.config import Settings, get_settings
from smartrss.db.engine import build_engine, build_sessionmaker
from smartrss.logging import configure_logging
from smartrss.security.auth import GlobalSlidingWindowRateLimiter
from smartrss.security.middleware import HostAllowListMiddleware, JsonMutationGuardMiddleware
from smartrss.security.safe_http import SSRFPolicy


def create_app(
    *,
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    ssrf_policy: SSRFPolicy | None = None,
    pair_rate_limiter: GlobalSlidingWindowRateLimiter | None = None,
) -> FastAPI:
    """Build and configure the SmartRSS FastAPI application."""
    cfg = settings or get_settings()
    configure_logging(cfg.log_level)

    owns_engine = engine is None
    app_engine = engine or build_engine(
        cfg.database_url,
        strict_sync_guard=cfg.synclog_strict_guard,
    )
    app_session_factory = session_factory or build_sessionmaker(
        app_engine,
        strict_sync_guard=cfg.synclog_strict_guard,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owns_engine:
                await app.state.engine.dispose()

    app = FastAPI(
        title="SmartRSS API",
        version=__version__,
        lifespan=lifespan,
    )

    app.state.settings = cfg
    app.state.engine = app_engine
    app.state.session_factory = app_session_factory
    app.state.ssrf_policy = ssrf_policy or SSRFPolicy(extra_blocked_cidrs=cfg.ssrf_blocked_cidrs)
    app.state.pair_rate_limiter = pair_rate_limiter or GlobalSlidingWindowRateLimiter()

    # Middleware registration (LAST added runs FIRST on incoming requests):
    # Execution order: HostAllowList -> CORS -> JsonMutationGuard -> Router
    app.add_middleware(JsonMutationGuardMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_allow_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.add_middleware(HostAllowListMiddleware, allowed_hosts=cfg.allowed_hosts)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(categories.router)
    app.include_router(feeds.router)
    app.include_router(items.router)
    app.include_router(system.router)

    return app
