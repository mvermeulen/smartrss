"""Real-PostgreSQL integration tests for Milestone 1d-ops feed service & CLI (T06..T08)."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import stat
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from typer.testing import CliRunner

import smartrss.cli as cli_mod
from smartrss.db.models import Category, Feed, Job, SyncChange
from smartrss.domain.opml import parse_opml
from smartrss.feeds.service import (
    MAX_OPML_BYTES,
    FeedAlreadySubscribedError,
    FeedCategoryNotFoundError,
    FeedValidationError,
    create_or_reactivate_feed,
    import_opml_subscriptions,
    list_feeds_query,
)
from smartrss.security.safe_http import SSRFPolicy


def _stub_policy() -> SSRFPolicy:
    async def _resolve(
        _host: str, _port: int
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        return [ipaddress.ip_address("93.184.216.34")]

    return SSRFPolicy(resolver=_resolve)


def _patch_cli_db(
    monkeypatch: pytest.MonkeyPatch,
    test_db_url: str,
) -> None:
    """Point smartrss CLI commands at the test PostgreSQL database with stubbed public DNS."""
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setattr(cli_mod, "SSRFPolicy", lambda **_kw: _stub_policy())


async def test_1d_ops_t06_feeds_add_list_cli_and_concurrent_category_race(
    test_db_url: str,
    db_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1d-ops-T06 (E-11, O-05, O-11): feeds add/list CLI, opt-in --create-category,
    forced-overlap concurrent category creation race (T6), --if-not-exists exit codes (0/3),
    SSRF validation, reactivation preserving fields (D3), and sync_changes emission."""
    _patch_cli_db(monkeypatch, test_db_url)
    runner = CliRunner()

    # (a) Missing category without --create-category -> exit 2 with hint
    res_missing = await asyncio.to_thread(
        runner.invoke,
        cli_mod.app,
        ["feeds", "add", "https://example.com/tech.xml", "--category", "Tech"],
    )
    assert res_missing.exit_code == 2
    assert "--create-category" in res_missing.output

    # With --create-category -> creates category + feed + poll job + sync_changes
    res_add = await asyncio.to_thread(
        runner.invoke,
        cli_mod.app,
        [
            "feeds",
            "add",
            "https://example.com/tech.xml",
            "--title",
            "Tech Blog",
            "--category",
            "Tech",
            "--create-category",
            "--poll-interval",
            "30",
            "--no-full-text",
            "--json",
        ],
    )
    assert res_add.exit_code == 0, res_add.output
    added = json.loads(res_add.output)
    assert added["created"] is True
    assert added["reactivated"] is False
    assert added["status"] == "active"
    assert added["category_id"] is not None
    feed_id = uuid.UUID(added["id"])
    cat_id = uuid.UUID(added["category_id"])

    async with session_factory() as s:
        feed = await s.get(Feed, feed_id)
        assert feed is not None
        assert feed.fetch_full_text is False
        assert feed.poll_interval_minutes == 30
        job = (
            await s.execute(
                sa.select(Job).where(Job.feed_id == feed_id, Job.job_type == "poll_feed")
            )
        ).scalar_one_or_none()
        assert job is not None
        changes = (
            (await s.execute(sa.select(SyncChange).order_by(SyncChange.seq.asc()))).scalars().all()
        )
        assert ("category", cat_id, "upsert") in [
            (c.entity_type, c.entity_id, c.op) for c in changes
        ]
        assert ("feed", feed_id, "upsert") in [(c.entity_type, c.entity_id, c.op) for c in changes]

    # (c) SSRF / paste-artifact URL rejected with exit 2
    res_bad = await asyncio.to_thread(
        runner.invoke,
        cli_mod.app,
        ["feeds", "add", "https://www.latent.space/feed](https://www.latent.space/feed"],
    )
    assert res_bad.exit_code == 2

    # (d) Duplicate active without --if-not-exists -> exit 1; with --if-not-exists -> exit 0
    res_dup = await asyncio.to_thread(
        runner.invoke,
        cli_mod.app,
        ["feeds", "add", "https://example.com/tech.xml"],
    )
    assert res_dup.exit_code == 1

    res_ine_active = await asyncio.to_thread(
        runner.invoke,
        cli_mod.app,
        ["feeds", "add", "https://example.com/tech.xml", "--if-not-exists", "--json"],
    )
    assert res_ine_active.exit_code == 0, res_ine_active.output
    ine_active_json = json.loads(res_ine_active.output)
    assert ine_active_json["created"] is False
    assert ine_active_json["status"] == "active"

    # (d2) O-11: --if-not-exists on paused feed -> exit 3, prints status, JSON status+created=false
    async with session_factory() as s:
        f_row = await s.get(Feed, feed_id)
        assert f_row is not None
        f_row.status = "paused"
        await s.flush()
        from smartrss.db.sync_log import SyncEvent, sync_log

        await sync_log.record(s, [SyncEvent("feed", feed_id, "upsert")])
        await s.commit()

    res_ine_paused = await asyncio.to_thread(
        runner.invoke,
        cli_mod.app,
        ["feeds", "add", "https://example.com/tech.xml", "--if-not-exists"],
    )
    assert res_ine_paused.exit_code == 3
    assert "status=paused" in res_ine_paused.output

    res_ine_paused_json = await asyncio.to_thread(
        runner.invoke,
        cli_mod.app,
        ["feeds", "add", "https://example.com/tech.xml", "--if-not-exists", "--json"],
    )
    assert res_ine_paused_json.exit_code == 3
    paused_data = json.loads(res_ine_paused_json.output)
    assert paused_data["created"] is False
    assert paused_data["status"] == "paused"

    # (e) Reactivating unsubscribed feed preserves fetch_full_text=False when omitted (D3)
    async with session_factory() as s:
        f_row = await s.get(Feed, feed_id)
        assert f_row is not None
        f_row.status = "unsubscribed"
        await s.flush()
        from smartrss.db.sync_log import SyncEvent, sync_log

        await sync_log.record(s, [SyncEvent("feed", feed_id, "upsert")])
        await s.commit()

    res_react = await asyncio.to_thread(
        runner.invoke,
        cli_mod.app,
        ["feeds", "add", "https://example.com/tech.xml", "--json"],
    )
    assert res_react.exit_code == 0, res_react.output
    react_data = json.loads(res_react.output)
    assert react_data["created"] is False
    assert react_data["reactivated"] is True
    assert react_data["status"] == "active"
    async with session_factory() as s:
        f_row = await s.get(Feed, feed_id)
        assert f_row is not None and f_row.fetch_full_text is False

    # feeds list (text & --json)
    res_list = await asyncio.to_thread(runner.invoke, cli_mod.app, ["feeds", "list"])
    assert res_list.exit_code == 0
    assert "https://example.com/tech.xml" in res_list.output
    res_list_json = await asyncio.to_thread(
        runner.invoke, cli_mod.app, ["feeds", "list", "--status", "active", "--json"]
    )
    assert res_list_json.exit_code == 0
    listed = json.loads(res_list_json.output)
    assert len(listed) == 1 and listed[0]["id"] == str(feed_id)

    # (b) O-05 forced-overlap real-PG concurrency race (T6 barrier):
    # Two sessions concurrently create the SAME new category name via create_or_reactivate_feed.
    policy = _stub_policy()
    both_inserted_or_waiting = asyncio.Barrier(2)

    async def _worker(url: str) -> uuid.UUID:
        async with session_factory() as s:
            # Force both transactions to run their initial SELECT FOR SHARE before either inserts
            await s.execute(
                sa.select(Category)
                .where(Category.deleted_at.is_(None))
                .order_by(Category.id.asc())
                .with_for_update(read=True)
            )
            await both_inserted_or_waiting.wait()
            feed_obj, _ = await create_or_reactivate_feed(
                s,
                policy,
                feed_url=url,
                category_name="RaceNews",
                create_category_if_missing=True,
            )
            await s.commit()
            assert feed_obj.category_id is not None
            return feed_obj.category_id

    cat_a, cat_b = await asyncio.gather(
        _worker("https://race-a.example.com/rss"),
        _worker("https://race-b.example.com/rss"),
    )
    assert cat_a == cat_b
    async with session_factory() as s:
        cats = (
            (
                await s.execute(
                    sa.select(Category).where(
                        sa.func.lower(Category.name) == "racenews",
                        Category.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(cats) == 1


async def test_1d_ops_t07_feeds_export_import_opml_roundtrip_and_portability(
    test_db_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1d-ops-T07 (E-11, O-04, S5, C8): export pure XML to stdout & mode-0600 file,
    re-import via stdin ('-') preserving muted/paused on same DB, fresh-DB portability import
    creates active defaults, oversized >2 MiB rejected, soft-deleted default-category rejected."""
    _patch_cli_db(monkeypatch, test_db_url)
    runner = CliRunner()

    # Seed 1 active feed (in category) + 1 muted feed (uncategorized) + 1 paused feed
    policy = _stub_policy()
    async with session_factory() as s:
        f1, _ = await create_or_reactivate_feed(
            s,
            policy,
            feed_url="https://alpha.example.com/rss",
            title="Alpha",
            category_name="Science",
            create_category_if_missing=True,
        )
        f2, _ = await create_or_reactivate_feed(
            s,
            policy,
            feed_url="https://muted.example.com/rss",
            title="Muted Feed",
            fetch_full_text=False,
        )
        f3, _ = await create_or_reactivate_feed(
            s,
            policy,
            feed_url="https://paused.example.com/rss",
            title="Paused Feed",
        )
        f2.status = "muted"
        f3.status = "paused"
        await s.flush()
        from smartrss.db.sync_log import SyncEvent, sync_log

        await sync_log.record(
            s, [SyncEvent("feed", f2.id, "upsert"), SyncEvent("feed", f3.id, "upsert")]
        )
        await s.commit()

    # 1. Export pure XML to stdout (zero log pollution)
    res_stdout = await asyncio.to_thread(runner.invoke, cli_mod.app, ["feeds", "export-opml"])
    assert res_stdout.exit_code == 0, res_stdout.output
    xml_out = res_stdout.output
    assert xml_out.lstrip().startswith("<?xml")
    subs = parse_opml(xml_out)
    assert {sub.feed_url for sub in subs} == {
        "https://alpha.example.com/rss",
        "https://muted.example.com/rss",
        "https://paused.example.com/rss",
    }
    by_url = {sub.feed_url: sub for sub in subs}
    assert by_url["https://alpha.example.com/rss"].category_name == "Science"

    # 2. Export to mode-0600 file via --output
    out_file = tmp_path / "exported.opml"
    res_file = await asyncio.to_thread(
        runner.invoke, cli_mod.app, ["feeds", "export-opml", "--output", str(out_file)]
    )
    assert res_file.exit_code == 0, res_file.output
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600
    assert out_file.read_text(encoding="utf-8") == xml_out

    # 3. Re-import via stdin ('-') onto SAME DB -> skipped_duplicates=3, muted/paused preserved!
    res_reimp = await asyncio.to_thread(
        runner.invoke,
        cli_mod.app,
        ["feeds", "import-opml", "-", "--json"],
        input=xml_out,
    )
    assert res_reimp.exit_code == 0, res_reimp.output
    summary_same = json.loads(res_reimp.output)
    assert summary_same == {
        "imported": 0,
        "reactivated": 0,
        "skipped_duplicates": 3,
        "rejected_invalid": 0,
    }
    async with session_factory() as s:
        rows = {f.feed_url: f for f in (await s.execute(sa.select(Feed))).scalars().all()}
        assert rows["https://muted.example.com/rss"].status == "muted"
        assert rows["https://paused.example.com/rss"].status == "paused"

    # 4. Portability contract (O-04): fresh DB import restores subscriptions & categories as active
    async with session_factory() as s:
        await s.execute(sa.text("TRUNCATE TABLE jobs, feeds, categories RESTART IDENTITY CASCADE"))
        await s.commit()

    res_fresh = await asyncio.to_thread(
        runner.invoke,
        cli_mod.app,
        ["feeds", "import-opml", str(out_file), "--json"],
    )
    assert res_fresh.exit_code == 0, res_fresh.output
    assert json.loads(res_fresh.output) == {
        "imported": 3,
        "reactivated": 0,
        "skipped_duplicates": 0,
        "rejected_invalid": 0,
    }
    async with session_factory() as s:
        fresh_feeds = {f.feed_url: f for f in (await s.execute(sa.select(Feed))).scalars().all()}
        assert len(fresh_feeds) == 3
        assert all(f.status == "active" for f in fresh_feeds.values())
        assert fresh_feeds["https://muted.example.com/rss"].fetch_full_text is True
        cat = (
            await s.execute(sa.select(Category).where(Category.name == "Science"))
        ).scalar_one_or_none()
        assert cat is not None
        assert fresh_feeds["https://alpha.example.com/rss"].category_id == cat.id

    # 5. S5: Oversized > 2 MiB OPML file rejected cleanly with exit 2
    huge_file = tmp_path / "huge.opml"
    huge_file.write_bytes(b"a" * (MAX_OPML_BYTES + 10))
    res_huge = await asyncio.to_thread(
        runner.invoke, cli_mod.app, ["feeds", "import-opml", str(huge_file)]
    )
    assert res_huge.exit_code == 2
    assert "exceeds" in res_huge.output

    # 6. C8: Soft-deleted --default-category rejected with exit 2
    deleted_cat_id = uuid.uuid4()
    async with session_factory() as s:
        now = (await s.execute(sa.select(sa.func.now()))).scalar_one()
        s.add(
            Category(
                id=deleted_cat_id,
                name="DeletedCat",
                sort_order=0,
                created_at=now,
                updated_at=now,
                deleted_at=now,
            )
        )
        await s.flush()
        from smartrss.db.sync_log import SyncEvent, sync_log

        await sync_log.record(s, [SyncEvent("category", deleted_cat_id, "delete")])
        await s.commit()

    res_del_cat = await asyncio.to_thread(
        runner.invoke,
        cli_mod.app,
        ["feeds", "import-opml", str(out_file), "--default-category", str(deleted_cat_id)],
    )
    assert res_del_cat.exit_code == 2

    # Also test invalid UTF-8 and missing file
    bad_utf8 = tmp_path / "bad.opml"
    bad_utf8.write_bytes(b"\xff\xfe\xfd")
    res_utf8 = await asyncio.to_thread(
        runner.invoke, cli_mod.app, ["feeds", "import-opml", str(bad_utf8)]
    )
    assert res_utf8.exit_code == 2

    res_nofile = await asyncio.to_thread(
        runner.invoke, cli_mod.app, ["feeds", "import-opml", str(tmp_path / "nope.opml")]
    )
    assert res_nofile.exit_code == 2

    # Service-level oversized payload guard
    async with session_factory() as s:
        with pytest.raises(ValueError, match="exceeds"):
            await import_opml_subscriptions(s, policy, opml_xml="x" * (MAX_OPML_BYTES + 1))


async def test_1d_ops_t08_api_routes_parity_with_shared_feed_service(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """1d-ops-T08: REST API feed routes remain in full parity after delegating to feeds.service."""
    client: httpx.AsyncClient = api_env["client"]

    # Create category, create feed, duplicate -> 409, list, export-opml, import-opml
    r_cat = await client.post("/api/v1/categories", json={"name": "Engineering"})
    assert r_cat.status_code == 201
    cat_id = r_cat.json()["id"]

    r_create = await client.post(
        "/api/v1/feeds",
        json={
            "feed_url": "https://eng.example.com/rss",
            "title": "Eng Blog",
            "category_id": cat_id,
        },
    )
    assert r_create.status_code == 201
    feed_id = r_create.json()["id"]

    r_dup = await client.post("/api/v1/feeds", json={"feed_url": "https://eng.example.com/rss"})
    assert r_dup.status_code == 409

    r_list = await client.get("/api/v1/feeds?status=active")
    assert r_list.status_code == 200
    assert any(row["id"] == feed_id for row in r_list.json())

    r_exp = await client.get("/api/v1/feeds/export-opml")
    assert r_exp.status_code == 200
    assert "https://eng.example.com/rss" in r_exp.text

    # Delete category -> create & OPML default_category_id return 422 (C8)
    r_del = await client.delete(f"/api/v1/categories/{cat_id}")
    assert r_del.status_code == 204
    r_bad_cat = await client.post(
        "/api/v1/feeds",
        json={"feed_url": "https://other.example.com/rss", "category_id": cat_id},
    )
    assert r_bad_cat.status_code == 422
    r_bad_opml_cat = await client.post(
        "/api/v1/feeds/import-opml",
        json={"opml_xml": r_exp.text, "default_category_id": cat_id},
    )
    assert r_bad_opml_cat.status_code == 422

    # Exercise remaining feeds.service branches (validation, reactivation fields, OPML reactivation)
    policy = _stub_policy()
    async with session_factory() as s:
        with pytest.raises(FeedValidationError, match="not both"):
            await create_or_reactivate_feed(
                s,
                policy,
                feed_url="https://both.example.com/rss",
                category_id=uuid.uuid4(),
                category_name="Tech",
            )
        with pytest.raises(FeedCategoryNotFoundError, match="empty"):
            await create_or_reactivate_feed(
                s,
                policy,
                feed_url="https://emptycat.example.com/rss",
                category_name="   ",
            )
        with pytest.raises(FeedValidationError):
            await import_opml_subscriptions(s, policy, opml_xml="<not-valid-xml")

        # Create active category + feed, match existing category, unsubscribe, then reactivate
        f_full, _ = await create_or_reactivate_feed(
            s,
            policy,
            feed_url="https://reactivate-full.example.com/rss",
            category_name="DeepTech",
            create_category_if_missing=True,
        )
        f_same_cat, _ = await create_or_reactivate_feed(
            s,
            policy,
            feed_url="https://samecat.example.com/rss",
            category_name="deeptech",
        )
        assert f_same_cat.category_id == f_full.category_id
        healthy_feeds = await list_feeds_query(s, health_state="healthy")
        assert len(healthy_feeds) >= 2

        from smartrss.db.sync_log import SyncEvent, sync_log

        f_full.status = "unsubscribed"
        f_same_cat.status = "unsubscribed"
        await s.flush()
        await sync_log.record(
            s,
            [
                SyncEvent("feed", f_full.id, "upsert"),
                SyncEvent("feed", f_same_cat.id, "upsert"),
            ],
        )
        await s.commit()

    async with session_factory() as s:
        f_re, was_re = await create_or_reactivate_feed(
            s,
            policy,
            feed_url="https://reactivate-full.example.com/rss",
            title="Reactivated Title",
            category_id=f_full.category_id,
            poll_interval_minutes=45,
            expiration_days_override=14,
            fetch_full_text=False,
            research_policy={
                "enabled": True,
                "fetch_pdfs": True,
                "guidance": "Check papers",
                "max_hops": 1,
            },
            curation_instructions="Prioritize benchmarks",
            fields_set={"fetch_full_text"},
        )
        assert was_re is True
        assert f_re.title == "Reactivated Title"
        assert f_re.poll_interval_minutes == 45
        assert f_re.expiration_days_override == 14
        assert f_re.fetch_full_text is False
        assert f_re.research_policy["enabled"] is True
        assert f_re.config_version == 2

        # OPML import reactivates unsubscribed feed & handles untitled feed
        opml_re = (
            '<?xml version="1.0"?><opml version="2.0"><body>'
            '<outline text="DeepTech">'
            '<outline text="Back Online" xmlUrl="https://samecat.example.com/rss"'
            ' htmlUrl="https://samecat.example.com/"/>'
            '<outline xmlUrl="https://untitled.example.com/feed.xml"/>'
            "</outline></body></opml>"
        )
        imp_re = await import_opml_subscriptions(s, policy, opml_xml=opml_re)
        await s.commit()
        assert imp_re.reactivated == 1
        assert imp_re.imported == 1


async def test_g06_concurrent_same_url_subscribe_raises_already_subscribed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """G-06 regression (forced-overlap real-PG race): two concurrent create_or_reactivate_feed
    calls for the same new feed_url — one creates, the other receives FeedAlreadySubscribedError
    instead of an unhandled IntegrityError."""
    policy = _stub_policy()
    race_url = "https://same-url-race.example.com/feed.xml"
    barrier = asyncio.Barrier(2)

    orig_execute = AsyncSession.execute

    async def _coordinated_execute(
        self: AsyncSession, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        result = await orig_execute(self, statement, *args, **kwargs)
        # Synchronize both sessions immediately after their initial SELECT Feed FOR UPDATE returns
        stmt_sql = str(statement)
        if (
            "FROM feeds" in stmt_sql
            and "FOR UPDATE" in stmt_sql
            and not self.in_nested_transaction()
            and not self.info.get("_g06_hit")
        ):
            self.info["_g06_hit"] = True
            await barrier.wait()
        return result

    async def _attempt() -> tuple[str, uuid.UUID]:
        async with session_factory() as s:
            try:
                feed_obj, _ = await create_or_reactivate_feed(s, policy, feed_url=race_url)
                await s.commit()
                return ("created", feed_obj.id)
            except FeedAlreadySubscribedError as exc:
                await s.rollback()
                return ("conflict", exc.existing_feed_id)

    mp = pytest.MonkeyPatch()
    mp.setattr(AsyncSession, "execute", _coordinated_execute)
    try:
        res_a, res_b = await asyncio.gather(_attempt(), _attempt())
    finally:
        mp.undo()

    outcomes = sorted([res_a[0], res_b[0]])
    assert outcomes == ["conflict", "created"]
    assert res_a[1] == res_b[1]

    async with session_factory() as s:
        count = (
            await s.execute(
                sa.select(sa.func.count()).select_from(Feed).where(Feed.feed_url == race_url)
            )
        ).scalar_one()
        assert count == 1


async def test_g06_f1_opml_import_concurrent_same_url_race_skips_duplicate_and_reactivates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """G-06-F1 regression: OPML import racing an uncommitted single subscribe on the same URL
    blocks on uq_feeds_feed_url, catches IntegrityError in a savepoint when the competitor commits,
    counts active/paused/muted as skipped_duplicates (or reactivates unsubscribed), and still
    imports all other subscriptions cleanly."""
    policy = _stub_policy()
    race_active_url = "https://opml-race-active.example.com/rss"
    other_url = "https://opml-race-other.example.com/rss"
    race_unsub_url = "https://opml-race-unsub.example.com/rss"

    async def _wait_for_blocked_insert(task: asyncio.Task[Any]) -> None:
        for _ in range(100):
            if task.done():
                task.result()  # Surface any unexpected early error immediately
                raise AssertionError("OPML import finished without blocking on uq_feeds_feed_url")
            # Fresh session per poll so Postgres never returns a cached pg_stat_activity snapshot
            async with session_factory() as s_probe:
                waiting = (
                    await s_probe.execute(
                        sa.text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = current_database() "
                            "AND pid <> pg_backend_pid() "
                            "AND wait_event_type = 'Lock' "
                            "AND state = 'active'"
                        )
                    )
                ).scalar_one()
            if waiting >= 1:
                return
            await asyncio.sleep(0.02)
        raise AssertionError("OPML import did not block on uq_feeds_feed_url as expected")

    # (a) Competitor inserts active feed; OPML import races on the same URL + another new URL
    opml_active = (
        '<?xml version="1.0"?><opml version="2.0"><body>'
        f'<outline text="Raced Active" xmlUrl="{race_active_url}"/>'
        f'<outline text="Other Feed" xmlUrl="{other_url}"/>'
        "</body></opml>"
    )

    async def _run_opml(xml: str) -> Any:
        async with session_factory() as s_a:
            summary = await import_opml_subscriptions(s_a, policy, opml_xml=xml)
            await s_a.commit()
            return summary

    import_task: asyncio.Task[Any] | None = None
    try:
        async with session_factory() as s_b:
            await create_or_reactivate_feed(
                s_b, policy, feed_url=race_active_url, title="Competitor"
            )
            # s_b has flushed race_active_url but NOT committed yet
            import_task = asyncio.create_task(_run_opml(opml_active))
            await _wait_for_blocked_insert(import_task)
            await s_b.commit()

        summary_a = await import_task
    finally:
        if import_task is not None and not import_task.done():
            import_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await import_task
    assert summary_a.imported == 1
    assert summary_a.skipped_duplicates == 1
    assert summary_a.reactivated == 0
    assert summary_a.rejected_invalid == 0

    async with session_factory() as s:
        active_cnt = (
            await s.execute(
                sa.select(sa.func.count()).select_from(Feed).where(Feed.feed_url == race_active_url)
            )
        ).scalar_one()
        other_cnt = (
            await s.execute(
                sa.select(sa.func.count()).select_from(Feed).where(Feed.feed_url == other_url)
            )
        ).scalar_one()
        assert active_cnt == 1
        assert other_cnt == 1

    # (b) Competitor inserts and transitions to unsubscribed before committing; OPML import
    # catches IntegrityError and reactivates cleanly
    opml_unsub = (
        '<?xml version="1.0"?><opml version="2.0"><body>'
        f'<outline text="Reactivated By OPML" xmlUrl="{race_unsub_url}"/>'
        "</body></opml>"
    )
    import_task2: asyncio.Task[Any] | None = None
    try:
        async with session_factory() as s_b2:
            f_unsub, _ = await create_or_reactivate_feed(s_b2, policy, feed_url=race_unsub_url)
            f_unsub.status = "unsubscribed"
            await s_b2.flush()
            import_task2 = asyncio.create_task(_run_opml(opml_unsub))
            await _wait_for_blocked_insert(import_task2)
            await s_b2.commit()

        summary_b = await import_task2
    finally:
        if import_task2 is not None and not import_task2.done():
            import_task2.cancel()
            with pytest.raises(asyncio.CancelledError):
                await import_task2
    assert summary_b.reactivated == 1
    assert summary_b.imported == 0
    assert summary_b.skipped_duplicates == 0
    async with session_factory() as s:
        row = (await s.execute(sa.select(Feed).where(Feed.feed_url == race_unsub_url))).scalar_one()
        assert row.status == "active"
        assert row.title == "Reactivated By OPML"
