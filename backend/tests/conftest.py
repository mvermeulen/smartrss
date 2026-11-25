"""Shared pytest fixtures for real PostgreSQL 16 (pgvector) integration tests."""

from __future__ import annotations

import ipaddress
import os
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from smartrss.api.app import create_app
from smartrss.config import Settings
from smartrss.db.engine import build_engine, build_sessionmaker
from smartrss.db.sync_log import bypass_sync_guard
from smartrss.security.auth import (
    GlobalSlidingWindowRateLimiter,
    create_pairing_code,
    redeem_pairing_code,
)
from smartrss.security.safe_http import FetchResult, SafeHttpClient, SSRFPolicy

DEFAULT_TEST_DB_URL = (
    "postgresql+psycopg://smartrss_test:smartrss_test@127.0.0.1:5433/smartrss_test"
)


def get_test_db_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DB_URL)


def make_alembic_config(database_url: str) -> AlembicConfig:
    backend_dir = Path(__file__).resolve().parents[1]
    cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


async def _test_dns_resolver(host: str, _port: int) -> list[ipaddress.IPv4Address]:
    lowered = host.lower()
    if lowered in ("localhost", "internal.local", "127.0.0.1"):
        return [ipaddress.IPv4Address("127.0.0.1")]
    if lowered.endswith(".lan") or lowered.endswith(".internal"):
        return [ipaddress.IPv4Address("192.168.1.1")]
    return [ipaddress.IPv4Address("93.184.216.34")]


@pytest.fixture(scope="session")
def test_db_url() -> str:
    return get_test_db_url()


@pytest.fixture(scope="session")
async def db_engine(test_db_url: str) -> AsyncIterator[AsyncEngine]:
    # Migrate test DB cleanly once per session
    alembic_command.upgrade(make_alembic_config(test_db_url), "head")
    engine = build_engine(test_db_url, strict_sync_guard=True, pool_size=10, max_overflow=10)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return build_sessionmaker(db_engine, strict_sync_guard=True)


@pytest.fixture(autouse=True)
async def _clean_db_for_integration(
    request: pytest.FixtureRequest,
) -> AsyncIterator[None]:
    """Reset all tables before each integration test while preserving singleton seeds."""
    if "integration" not in str(request.fspath):
        yield
        return

    engine: AsyncEngine = request.getfixturevalue("db_engine")
    with bypass_sync_guard():
        async with engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "TRUNCATE TABLE stage_metrics, jobs, pipeline_runs, sync_mutations, items, "
                    "feeds, categories, sync_changes, pairing_codes, devices "
                    "RESTART IDENTITY CASCADE"
                )
            )
            await conn.execute(
                sa.text(
                    "UPDATE sync_state SET current_seq = 0, pruned_through_seq = 0, "
                    "instance_epoch = gen_random_uuid(), updated_at = now() WHERE id = 1"
                )
            )
            await conn.execute(
                sa.text(
                    "UPDATE app_settings SET default_expiration_days = 30, backfill_cap = 10, "
                    "default_poll_interval_minutes = 60, last_retention_cleanup_at = NULL, "
                    "updated_at = now() WHERE id = 1"
                )
            )
    yield


@pytest.fixture
def test_ssrf_policy() -> SSRFPolicy:
    return SSRFPolicy(resolver=_test_dns_resolver)


@pytest.fixture
async def api_env(
    db_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    test_ssrf_policy: SSRFPolicy,
) -> AsyncIterator[dict[str, Any]]:
    """Build FastAPI app + httpx ASGITransport client + paired device token."""
    settings = Settings(
        DATABASE_URL=get_test_db_url(),
        ALLOWED_HOSTS="localhost,localhost:8080,127.0.0.1,127.0.0.1:8080",
        CORS_ALLOW_ORIGINS="http://localhost:8080",
        SYNCLOG_STRICT_GUARD=True,
    )
    rate_limiter = GlobalSlidingWindowRateLimiter(max_attempts=5, window_seconds=300.0)
    app = create_app(
        settings=settings,
        engine=db_engine,
        session_factory=session_factory,
        ssrf_policy=test_ssrf_policy,
        pair_rate_limiter=rate_limiter,
    )

    async with session_factory() as session:
        created = await create_pairing_code(session, device_name="TestLaptop", platform="ubuntu")
        paired = await redeem_pairing_code(session, raw_code=created.code)
        await session.commit()
        token = paired.bearer_token
        device_id = paired.device.id

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://localhost:8080",
        headers={
            "Host": "localhost:8080",
            "Authorization": f"Bearer {token}",
        },
    ) as client:
        yield {
            "app": app,
            "client": client,
            "token": token,
            "device_id": device_id,
            "rate_limiter": rate_limiter,
            "transport": transport,
        }


class StubSafeHttpClient(SafeHttpClient):
    """Deterministic SafeHttpClient subclass for stage integration tests."""

    def __init__(
        self,
        handler: Callable[[str, dict[str, str]], FetchResult | Exception],
        *,
        policy: SSRFPolicy | None = None,
    ) -> None:
        self.policy = policy or SSRFPolicy(resolver=_test_dns_resolver)
        self._handler = handler
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def aclose(self) -> None:
        return None

    async def fetch(
        self,
        url: str,
        *,
        headers: Any = None,
        max_bytes: int | None = None,
        total_timeout: float | None = None,
    ) -> FetchResult:
        hdrs = dict(headers or {})
        self.calls.append((url, hdrs))
        outcome = self._handler(url, hdrs)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_rss_xml(
    title: str,
    entries: list[dict[str, str | None]],
    *,
    site_url: str = "https://blog.example.com",
) -> bytes:
    """Build a minimal valid RSS 2.0 XML payload."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel>',
        f"<title>{title}</title>",
        f"<link>{site_url}</link>",
        "<description>Test feed</description>",
    ]
    for e in entries:
        parts.append("<item>")
        if e.get("title") is not None:
            parts.append(f"<title>{e['title']}</title>")
        if e.get("link") is not None:
            parts.append(f"<link>{e['link']}</link>")
        if e.get("guid") is not None:
            guid_val = e["guid"]
            if guid_val.startswith(("http://", "https://")):
                parts.append(f"<guid>{guid_val}</guid>")
            else:
                parts.append(f'<guid isPermaLink="false">{guid_val}</guid>')
        if e.get("summary") is not None:
            parts.append(f"<description><![CDATA[{e['summary']}]]></description>")
        if e.get("pubDate") is not None:
            parts.append(f"<pubDate>{e['pubDate']}</pubDate>")
        if e.get("author") is not None:
            parts.append(f"<author>{e['author']}</author>")
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "\n".join(parts).encode("utf-8")


def random_uuid() -> uuid.UUID:
    return uuid.uuid4()
