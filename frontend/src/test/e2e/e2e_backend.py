"""Real-PostgreSQL E2E fixture & verification helper for Milestone 1c Playwright tests."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from smartrss.db.engine import build_engine, build_sessionmaker
from smartrss.db.models import (
    Category,
    Device,
    Feed,
    Item,
    Job,
    PipelineRun,
    StageMetric,
)
from smartrss.db.sync_log import SyncEvent, bypass_sync_guard, sync_log
from smartrss.search.fts import reindex_all_items
from smartrss.security.auth import create_pairing_code

DEFAULT_TEST_DB_URL = (
    "postgresql+psycopg://smartrss_test:smartrss_test@127.0.0.1:5433/smartrss_test"
)


def get_db_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DB_URL)


def ensure_migrated(db_url: str) -> None:
    backend_dir = Path(__file__).resolve().parents[4] / "backend"
    cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_command.upgrade(cfg, "head")


async def cmd_reset_and_seed(args: argparse.Namespace) -> dict[str, Any]:
    db_url = get_db_url()
    ensure_migrated(db_url)
    engine = build_engine(db_url, strict_sync_guard=True)
    factory = build_sessionmaker(engine, strict_sync_guard=True)
    try:
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

        now = datetime.now(UTC)
        out: dict[str, Any] = {
            "category_ids": {},
            "feed_ids": {},
            "item_ids": [],
            "dead_letter_job_ids": [],
        }

        async with factory() as session:
            cat_tech = Category(id=uuid.uuid4(), name="Tech", sort_order=0)
            cat_science = Category(id=uuid.uuid4(), name="Science", sort_order=1)
            cat_temp = Category(id=uuid.uuid4(), name="TempCategory", sort_order=2)
            session.add_all([cat_tech, cat_science, cat_temp])
            await session.flush()

            feed_alpha = Feed(
                id=uuid.uuid4(),
                category_id=cat_tech.id,
                feed_url="https://alpha.example.com/rss.xml",
                site_url="https://alpha.example.com",
                title="Alpha Engineering",
                status="active",
                health_state="healthy",
                poll_interval_minutes=60,
                last_fetched_at=now - timedelta(minutes=10),
                last_success_at=now - timedelta(minutes=10),
                curation_instructions="Focus on distributed systems.",
                config_version=1,
            )
            feed_beta = Feed(
                id=uuid.uuid4(),
                category_id=cat_science.id,
                feed_url="https://beta.example.com/feed.xml",
                site_url="https://beta.example.com",
                title="Beta Science",
                status="active",
                health_state="degraded" if args.health_fixtures else "healthy",
                consecutive_errors=3 if args.health_fixtures else 0,
                last_error="HTTP 503 Upstream Timeout" if args.health_fixtures else None,
                poll_interval_minutes=60,
                last_fetched_at=now - timedelta(minutes=25),
                last_success_at=now - timedelta(hours=4),
                curation_instructions="",
                config_version=1,
            )
            feeds_to_add = [feed_alpha, feed_beta]
            feed_broken: Feed | None = None
            if args.health_fixtures:
                feed_broken = Feed(
                    id=uuid.uuid4(),
                    category_id=cat_tech.id,
                    feed_url="https://broken.example.com/atom.xml",
                    site_url="https://broken.example.com",
                    title="Broken Wire",
                    status="active",
                    health_state="broken",
                    consecutive_errors=8,
                    last_error="DNS lookup failed for broken.example.com",
                    poll_interval_minutes=60,
                    last_fetched_at=now - timedelta(minutes=5),
                    last_success_at=now - timedelta(days=3),
                    curation_instructions="",
                    config_version=1,
                )
                feeds_to_add.append(feed_broken)

            session.add_all(feeds_to_add)
            await session.flush()

            events: list[SyncEvent] = [
                SyncEvent("category", cat_tech.id, "upsert"),
                SyncEvent("category", cat_science.id, "upsert"),
                SyncEvent("category", cat_temp.id, "upsert"),
                SyncEvent("feed", feed_alpha.id, "upsert"),
                SyncEvent("feed", feed_beta.id, "upsert"),
            ]
            if feed_broken is not None:
                events.append(SyncEvent("feed", feed_broken.id, "upsert"))

            items_to_add: list[Item] = []

            if args.hostile:
                hostile_html = (
                    "<p>Hostile article body</p>"
                    '<script>window.__xssExecuted = "script";</script>'
                    '<img src="https://blocked.invalid/pixel.gif" alt="Blocked Diagram" '
                    "onerror=\"window.__xssExecuted = 'img'\">"
                    '<svg onload="window.__xssExecuted = \'svg\'"><circle r="5"/></svg>'
                    "<math><mtext><table><mglyph><style><!--</style>"
                    "<img title=\"--&gt;&lt;img src=1 onerror=window.__xssExecuted='mxss'&gt;\">"
                    "</mglyph></table></mtext></math>"
                    "<a href=\"javascript:window.__xssExecuted='js'\">Evil JS Link</a> "
                    '<a href="/2026/11/safe-relative">Safe Relative Link</a>'
                )
                hostile_item = Item(
                    id=uuid.uuid4(),
                    feed_id=feed_alpha.id,
                    guid="hostile-guid-1",
                    raw_url="https://alpha.example.com/2026/11/hostile",
                    canonical_url="https://alpha.example.com/2026/11/hostile",
                    title="Hostile <script>alert(1)</script> Security Probe",
                    author="Mallory",
                    published_at=now - timedelta(minutes=1),
                    fetched_at=now - timedelta(minutes=1),
                    raw_summary=hostile_html,
                    extracted_text="Safe plain extracted text with <script>ignored()</script>.",
                    extraction_status="ok",
                    state="unread",
                    is_starred=False,
                    read_state_updated_at=now - timedelta(minutes=1),
                    starred_updated_at=now - timedelta(minutes=1),
                )
                items_to_add.append(hostile_item)

            count = max(0, int(args.items))
            for idx in range(count):
                target_feed = feed_alpha if idx % 2 == 0 else feed_beta
                ts = now - timedelta(minutes=idx + 2)
                topic = "PostgreSQL" if idx % 3 == 0 else "Distributed"
                summary_topic = (
                    "SQLite and PostgreSQL sync" if idx == 0 else "offline-first caching"
                )
                item = Item(
                    id=uuid.uuid4(),
                    feed_id=target_feed.id,
                    guid=f"item-guid-{idx}",
                    raw_url=f"https://example.com/articles/{idx}",
                    canonical_url=f"https://example.com/articles/{idx}",
                    title=f"Article {idx:03d}: {topic} Systems Note",
                    author="Alice" if idx % 2 == 0 else "Bob",
                    published_at=ts,
                    fetched_at=ts,
                    raw_summary=(
                        f"<p>Summary paragraph for article {idx} discussing {summary_topic}.</p>"
                    ),
                    extracted_text=f"Full extracted text for article {idx}.",
                    extraction_status="ok",
                    state="unread",
                    is_starred=(idx == 1),
                    starred_at=(ts if idx == 1 else None),
                    read_state_updated_at=ts,
                    starred_updated_at=ts,
                )
                items_to_add.append(item)

            if args.include_expired:
                exp_ts = now - timedelta(days=45)
                expired_item = Item(
                    id=uuid.uuid4(),
                    feed_id=feed_alpha.id,
                    guid="expired-archive-guid",
                    raw_url="https://alpha.example.com/archive/old-post",
                    canonical_url="https://alpha.example.com/archive/old-post",
                    title="Archived Quantum Consensus Deep Dive",
                    author="Archive Author",
                    published_at=exp_ts,
                    fetched_at=exp_ts,
                    raw_summary="<p>Historical archived article from 45 days ago.</p>",
                    extracted_text="Historical quantum consensus archived text.",
                    extraction_status="ok",
                    state="expired",
                    is_starred=False,
                    expired_at=now - timedelta(days=15),
                    read_state_updated_at=exp_ts,
                    starred_updated_at=exp_ts,
                )
                items_to_add.append(expired_item)

            session.add_all(items_to_add)
            await session.flush()

            for item in items_to_add:
                events.append(SyncEvent("item", item.id, "upsert"))

            await sync_log.record(session, events)

            if args.health_fixtures:
                run1 = PipelineRun(
                    trigger="scheduler_window",
                    stages=["poll_feed", "extract_content"],
                    status="completed",
                    started_at=now - timedelta(minutes=30),
                    finished_at=now - timedelta(minutes=29),
                    duration_ms=4200,
                    items_polled=12,
                    items_extracted=10,
                    jobs_failed=0,
                    total_llm_ms=0,
                )
                run2 = PipelineRun(
                    trigger="manual",
                    stages=["poll_feed", "extract_content"],
                    status="partial",
                    started_at=now - timedelta(hours=2),
                    finished_at=now - timedelta(hours=2) + timedelta(seconds=8),
                    duration_ms=8100,
                    items_polled=8,
                    items_extracted=6,
                    jobs_failed=2,
                    total_llm_ms=0,
                )
                session.add_all([run1, run2])
                await session.flush()

                metrics = [
                    StageMetric(
                        run_id=run1.id,
                        job_type="poll_feed",
                        feed_id=feed_alpha.id,
                        status="ok",
                        queue_wait_ms=15,
                        duration_ms=220,
                        created_at=now - timedelta(minutes=29),
                    ),
                    StageMetric(
                        run_id=run1.id,
                        job_type="poll_feed",
                        feed_id=feed_beta.id,
                        status="failed",
                        queue_wait_ms=20,
                        duration_ms=510,
                        created_at=now - timedelta(minutes=28),
                    ),
                    StageMetric(
                        run_id=run1.id,
                        job_type="extract_content",
                        feed_id=feed_alpha.id,
                        status="ok",
                        queue_wait_ms=30,
                        duration_ms=840,
                        created_at=now - timedelta(minutes=27),
                    ),
                    StageMetric(
                        run_id=run2.id,
                        job_type="poll_feed",
                        feed_id=feed_alpha.id,
                        status="ok",
                        queue_wait_ms=10,
                        duration_ms=180,
                        created_at=now - timedelta(days=5),
                    ),
                ]
                session.add_all(metrics)

                dl_retry = Job(
                    job_type="poll_feed",
                    dedupe_key=f"poll_feed:{feed_beta.id}:dl1",
                    payload={"feed_id": str(feed_beta.id)},
                    feed_id=feed_beta.id,
                    status="failed",
                    attempts=5,
                    max_attempts=5,
                    last_error="HTTP 503 after 5 attempts",
                )
                dl_dismiss = Job(
                    job_type="extract_content",
                    dedupe_key=f"extract_content:{items_to_add[0].id}:dl2",
                    payload={"item_id": str(items_to_add[0].id)},
                    feed_id=feed_alpha.id,
                    item_id=items_to_add[0].id,
                    status="failed",
                    attempts=5,
                    max_attempts=5,
                    last_error="Extractor subprocess timeout",
                )
                session.add_all([dl_retry, dl_dismiss])
                await session.flush()
                out["dead_letter_job_ids"] = [dl_retry.id, dl_dismiss.id]

            pairing = await create_pairing_code(
                session, device_name="E2E Ubuntu Laptop", platform="ubuntu"
            )
            await session.commit()

            out["pairing_code"] = pairing.code
            out["category_ids"] = {
                "tech": str(cat_tech.id),
                "science": str(cat_science.id),
                "temp": str(cat_temp.id),
            }
            out["feed_ids"] = {
                "alpha": str(feed_alpha.id),
                "beta": str(feed_beta.id),
            }
            if feed_broken is not None:
                out["feed_ids"]["broken"] = str(feed_broken.id)
            out["item_ids"] = [str(i.id) for i in items_to_add]

        # Populate PostgreSQL FTS search_vector for connected search tests
        await reindex_all_items(factory)

        return out
    finally:
        await engine.dispose()


async def cmd_pair_code(args: argparse.Namespace) -> dict[str, Any]:
    engine = build_engine(get_db_url(), strict_sync_guard=True)
    factory = build_sessionmaker(engine, strict_sync_guard=True)
    try:
        async with factory() as session:
            created = await create_pairing_code(
                session, device_name=args.name, platform=args.platform
            )
            await session.commit()
            return {"code": created.code}
    finally:
        await engine.dispose()


async def cmd_delete_category(args: argparse.Namespace) -> dict[str, Any]:
    engine = build_engine(get_db_url(), strict_sync_guard=True)
    factory = build_sessionmaker(engine, strict_sync_guard=True)
    try:
        cat_id = uuid.UUID(args.category_id)
        async with factory() as session:
            cat = await session.get(Category, cat_id)
            if cat is None:
                raise RuntimeError(f"Category not found: {cat_id}")
            await session.delete(cat)
            await sync_log.record(session, [SyncEvent("category", cat_id, "delete")])
            await session.commit()
            return {"deleted": str(cat_id)}
    finally:
        await engine.dispose()


async def cmd_bump_feed_version(args: argparse.Namespace) -> dict[str, Any]:
    engine = build_engine(get_db_url(), strict_sync_guard=True)
    factory = build_sessionmaker(engine, strict_sync_guard=True)
    try:
        feed_id = uuid.UUID(args.feed_id)
        async with factory() as session:
            feed = await session.get(Feed, feed_id)
            if feed is None:
                raise RuntimeError(f"Feed not found: {feed_id}")
            feed.curation_instructions = args.instructions
            feed.config_version += 1
            feed.updated_at = datetime.now(UTC)
            await sync_log.record(session, [SyncEvent("feed", feed.id, "upsert")])
            await session.commit()
            return {"feed_id": str(feed.id), "config_version": feed.config_version}
    finally:
        await engine.dispose()


async def cmd_query_state(_args: argparse.Namespace) -> dict[str, Any]:
    engine = build_engine(get_db_url(), strict_sync_guard=True)
    factory = build_sessionmaker(engine, strict_sync_guard=True)
    try:
        async with factory() as session:
            items = (await session.execute(sa.select(Item))).scalars().all()
            feeds = (await session.execute(sa.select(Feed))).scalars().all()
            jobs = (await session.execute(sa.select(Job))).scalars().all()
            devices = (await session.execute(sa.select(Device))).scalars().all()
            return {
                "items": {
                    str(i.id): {"state": i.state, "is_starred": i.is_starred, "title": i.title}
                    for i in items
                },
                "feeds": {
                    str(f.id): {
                        "title": f.title,
                        "status": f.status,
                        "category_id": str(f.category_id) if f.category_id else None,
                        "config_version": f.config_version,
                        "curation_instructions": f.curation_instructions,
                    }
                    for f in feeds
                },
                "jobs": {str(j.id): {"status": j.status, "job_type": j.job_type} for j in jobs},
                "devices": {
                    str(d.id): {
                        "name": d.name,
                        "revoked_at": d.revoked_at.isoformat() if d.revoked_at else None,
                    }
                    for d in devices
                },
            }
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="E2E backend helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_seed = sub.add_parser("reset-and-seed")
    p_seed.add_argument("--items", type=int, default=6)
    p_seed.add_argument("--hostile", action="store_true")
    p_seed.add_argument("--health-fixtures", action="store_true")
    p_seed.add_argument("--include-expired", action="store_true")

    p_pair = sub.add_parser("pair-code")
    p_pair.add_argument("--name", default="Second Device")
    p_pair.add_argument("--platform", default="ubuntu")

    p_del_cat = sub.add_parser("delete-category")
    p_del_cat.add_argument("category_id")

    p_bump = sub.add_parser("bump-feed-version")
    p_bump.add_argument("feed_id")
    p_bump.add_argument("--instructions", required=True)

    sub.add_parser("query-state")

    args = parser.parse_args()
    handlers = {
        "reset-and-seed": cmd_reset_and_seed,
        "pair-code": cmd_pair_code,
        "delete-category": cmd_delete_category,
        "bump-feed-version": cmd_bump_feed_version,
        "query-state": cmd_query_state,
    }
    res = asyncio.run(handlers[args.cmd](args))
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
