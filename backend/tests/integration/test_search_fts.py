"""Integration tests for PostgreSQL FTS ranking, filters, hooks, reindex, & snippets (1b-T10)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import StubSafeHttpClient, make_rss_xml
from typer.testing import CliRunner

from smartrss.cli import app as cli_app
from smartrss.db.models import Category, Feed, Item, SyncState
from smartrss.db.sync_log import SyncEvent, bypass_sync_guard, sync_log
from smartrss.search.fts import rebuild_item_search_vector, reindex_all_items
from smartrss.security.safe_http import DomainRateLimiter, FetchResult
from smartrss.worker.stages.extract_content import run_extract_content
from smartrss.worker.stages.poll_feed import run_poll_feed


async def test_1b_t10_fts_auth_weights_queries_filters_and_xss_safe_snippets(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify auth, A>B>C>D weighting, negation-only 422, facets, slim DTO, & safe snippets."""
    client = api_env["client"]
    transport = api_env["transport"]

    # 1. Unauthenticated request rejected with 401 (P-17)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://localhost:8080",
        headers={"Host": "localhost:8080"},
    ) as unauth:
        r_unauth = await unauth.get("/api/v1/search", params={"q": "pgvector"})
        assert r_unauth.status_code == 401

    now = datetime.now(UTC)
    cat_a = uuid.uuid4()
    cat_b = uuid.uuid4()
    feed_a = uuid.uuid4()
    feed_b = uuid.uuid4()
    item_title_hit = uuid.uuid4()
    item_body_hit = uuid.uuid4()
    item_xss_expired = uuid.uuid4()

    filler = " ".join(f"fillerword{i}" for i in range(60))
    multi_frag_body = (
        f"Intro … <script>alert(1)</script> \x02\x03\x04 pgvector indexing speeds up search. "
        f"{filler} "
        f"Later section … tuning pgvector recall in production."
    )

    async with session_factory() as s:
        s.add_all([Category(id=cat_a, name="DB"), Category(id=cat_b, name="Other")])
        await s.flush()
        s.add_all(
            [
                Feed(
                    id=feed_a,
                    category_id=cat_a,
                    feed_url="https://fts-a.example.com/rss",
                    title="FTS A",
                ),
                Feed(
                    id=feed_b,
                    category_id=cat_b,
                    feed_url="https://fts-b.example.com/rss",
                    title="FTS B",
                ),
            ]
        )
        await s.flush()
        items = [
            Item(
                id=item_title_hit,
                feed_id=feed_a,
                guid="t1",
                raw_url="https://fts.example.com/1",
                canonical_url="https://fts.example.com/1",
                title="Pgvector Deep Dive",
                author="Alice",
                published_at=now - timedelta(days=2),
                fetched_at=now - timedelta(days=2),
                raw_summary="<p>About sync watermark</p><script>hiddenjsword</script>",
                extracted_text="General database article",
                state="unread",
                is_starred=True,
                starred_at=now - timedelta(days=1),
                read_state_updated_at=now - timedelta(days=2),
                starred_updated_at=now - timedelta(days=1),
            ),
            Item(
                id=item_body_hit,
                feed_id=feed_a,
                guid="t2",
                raw_url="https://fts.example.com/2",
                canonical_url="https://fts.example.com/2",
                title="General Architecture Notes",
                author="Bob",
                published_at=now - timedelta(days=1),
                fetched_at=now - timedelta(days=1),
                raw_summary="<p>Overview</p>",
                extracted_text="Mentions pgvector once in body and mysql too",
                state="read",
                read_at=now - timedelta(hours=12),
                read_state_updated_at=now - timedelta(hours=12),
                starred_updated_at=now - timedelta(days=1),
            ),
            Item(
                id=item_xss_expired,
                feed_id=feed_b,
                guid="t3",
                raw_url="https://fts.example.com/3",
                canonical_url="https://fts.example.com/3",
                title="Archived Security Post",
                author="Carol",
                published_at=now - timedelta(days=45),
                fetched_at=now - timedelta(days=45),
                raw_summary="<p><b>Archived</b> summary</p>",
                extracted_text=multi_frag_body,
                state="expired",
                expired_at=now - timedelta(days=15),
                read_state_updated_at=now - timedelta(days=15),
                starred_updated_at=now - timedelta(days=45),
            ),
        ]
        s.add_all(items)
        await s.flush()
        for it in items:
            await rebuild_item_search_vector(
                s,
                item_id=it.id,
                title=it.title,
                author=it.author,
                raw_summary=it.raw_summary,
                extracted_text=it.extracted_text,
            )
        await sync_log.record(
            s,
            [
                SyncEvent("category", cat_a, "upsert"),
                SyncEvent("category", cat_b, "upsert"),
                SyncEvent("feed", feed_a, "upsert"),
                SyncEvent("feed", feed_b, "upsert"),
                *[SyncEvent("item", it.id, "upsert") for it in items],
            ],
        )
        await s.commit()

    # 2. A > D weighting: title match outranks body match under sort=relevance
    r_rel = await client.get("/api/v1/search", params={"q": "pgvector", "sort": "relevance"})
    assert r_rel.status_code == 200
    body_rel = r_rel.json()
    assert body_rel["total"] == 3
    assert body_rel["results"][0]["item"]["id"] == str(item_title_hit)
    assert body_rel["results"][0]["rank"] > 0
    # Slim SearchItemDTO omits raw_summary & summary_truncated (P-08)
    assert "raw_summary" not in body_rel["results"][0]["item"]
    assert "summary_truncated" not in body_rel["results"][0]["item"]

    # 3. Phrase + exclusion (websearch_to_tsquery)
    r_phrase = await client.get("/api/v1/search", params={"q": '"sync watermark"'})
    assert r_phrase.status_code == 200
    assert [x["item"]["id"] for x in r_phrase.json()["results"]] == [str(item_title_hit)]

    r_excl = await client.get("/api/v1/search", params={"q": "pgvector -mysql"})
    assert r_excl.status_code == 200
    excl_ids = {x["item"]["id"] for x in r_excl.json()["results"]}
    assert str(item_title_hit) in excl_ids
    assert str(item_body_hit) not in excl_ids

    # 4. HTML <script> content in raw_summary is stripped (not indexed)
    r_script = await client.get("/api/v1/search", params={"q": "hiddenjsword"})
    assert r_script.status_code == 200
    assert r_script.json()["total"] == 0

    # 5. Stop-words & punctuation return empty cleanly; blank q falls back to newest
    for empty_q in ("the and", "!!!"):
        r_empty = await client.get("/api/v1/search", params={"q": empty_q})
        assert r_empty.status_code == 200
        assert r_empty.json()["total"] == 0
        assert r_empty.json()["results"] == []

    r_blank = await client.get("/api/v1/search", params={"q": "   ", "sort": "relevance"})
    assert r_blank.status_code == 200
    assert r_blank.json()["sort"] == "newest"
    assert r_blank.json()["total"] == 3

    # 6. Negation-only / non-indexable queries return 422 (P-13)
    for bad_q in ("-foo", "-foo -bar", '-"hello world"', "foo OR -bar"):
        r_neg = await client.get("/api/v1/search", params={"q": bad_q})
        assert r_neg.status_code == 422

    # 7. Facet filters (category_id, feed_id, state='expired', is_starred, date_from/date_to)
    r_cat = await client.get(
        "/api/v1/search",
        params={"q": "pgvector", "category_id": str(cat_a), "is_starred": "true"},
    )
    assert [x["item"]["id"] for x in r_cat.json()["results"]] == [str(item_title_hit)]

    r_exp = await client.get(
        "/api/v1/search",
        params={
            "q": "pgvector",
            "feed_id": str(feed_b),
            "state": "expired",
            "date_from": (now - timedelta(days=50)).isoformat(),
            "date_to": (now - timedelta(days=30)).isoformat(),
            "sort": "newest",
        },
    )
    assert r_exp.status_code == 200
    assert r_exp.json()["total"] == 1
    hit_xss = r_exp.json()["results"][0]
    assert hit_xss["item"]["id"] == str(item_xss_expired)

    # 8. Multi-fragment XSS-safe snippet splits on EOT and emits zero HTML highlight tags
    snippet_groups = hit_xss["snippet"]
    assert isinstance(snippet_groups, list)
    assert len(snippet_groups) == 2  # Two distinct fragments!
    all_runs = [run for group in snippet_groups for run in group]
    assert any(run["highlight"] is True and "pgvector" in run["text"].lower() for run in all_runs)
    # Control sentinels stripped, zero <mark>/<b>/<script> tags emitted
    joined_text = "".join(run["text"] for run in all_runs)
    assert "\x02" not in joined_text and "\x03" not in joined_text and "\x04" not in joined_text
    assert "<mark>" not in joined_text and "<script>" not in joined_text
    assert "alert(1)" in joined_text
    assert "…" in joined_text

    # Naive date_from rejected with 422
    r_naive = await client.get("/api/v1/search", params={"date_from": "2026-11-01T00:00:00"})
    assert r_naive.status_code == 422


async def test_1b_t10_poll_extract_hooks_and_search_reindex_cli(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
    test_db_url: str,
    monkeypatch: Any,
) -> None:
    """poll_feed & extract_content maintain search_vector; reindex CLI emits 0 sync_changes."""
    client = api_env["client"]
    feed_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(
            Feed(
                id=feed_id,
                feed_url="https://hooks.example.com/rss",
                title="Hooks Feed",
                initial_backfill_done=True,
                fetch_full_text=True,
            )
        )
        await s.flush()
        await sync_log.record(s, [SyncEvent("feed", feed_id, "upsert")])
        await s.commit()

    # poll_feed populates search_vector on insert
    rss_xml = make_rss_xml(
        "Hooks Feed",
        [
            {
                "guid": "h1",
                "title": "PollHook Nebula",
                "link": "https://hooks.example.com/article",
                "summary": "<p>Summary with pulsar</p>",
            }
        ],
    )
    stub_poll = StubSafeHttpClient(
        lambda url, _h: FetchResult(
            status_code=200,
            final_url=url,
            headers={"content-type": "application/rss+xml"},
            body_bytes=rss_xml,
            content_type="application/rss+xml",
            permanent_redirect_url=None,
        )
    )
    await run_poll_feed(session_factory, stub_poll, feed_id=feed_id)

    r_poll = await client.get("/api/v1/search", params={"q": "nebula"})
    assert r_poll.status_code == 200
    assert r_poll.json()["total"] == 1
    item_id = uuid.UUID(r_poll.json()["results"][0]["item"]["id"])

    # extract_content rebuilds search_vector with extracted_text
    long_article_html = (
        b"<html><body><article><h1>PollHook Nebula</h1>"
        b"<p>This full article discusses magnetar astrophysics in thorough detail so "
        b"trafilatura extracts well over one hundred characters of clean body prose "
        b"for indexing by PostgreSQL full-text search.</p></article></body></html>"
    )
    stub_ext = StubSafeHttpClient(
        lambda url, _h: FetchResult(
            status_code=200,
            final_url=url,
            headers={"content-type": "text/html; charset=utf-8"},
            body_bytes=long_article_html,
            content_type="text/html; charset=utf-8",
            permanent_redirect_url=None,
        )
    )
    ext_out = await run_extract_content(
        session_factory, stub_ext, DomainRateLimiter(min_interval_seconds=0.0), item_id=item_id
    )
    assert ext_out.extraction_status == "ok"

    r_ext = await client.get("/api/v1/search", params={"q": "magnetar"})
    assert r_ext.status_code == 200
    assert r_ext.json()["total"] == 1

    # Clear search_vector manually, then verify reindex_all_items & CLI rebuild with 0 sync events
    with bypass_sync_guard():
        async with session_factory() as s:
            await s.execute(sa.update(Item).values(search_vector=None))
            seq_before = (await s.get(SyncState, 1)).current_seq  # type: ignore[union-attr]
            await s.commit()

    count = await reindex_all_items(session_factory, fts_language="english", batch_size=10)
    assert count == 1

    async with session_factory() as s:
        assert (await s.get(SyncState, 1)).current_seq == seq_before  # type: ignore[union-attr]

    r_after = await client.get("/api/v1/search", params={"q": "magnetar"})
    assert r_after.json()["total"] == 1

    # Also exercise CLI `smartrss search reindex`
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    from smartrss.config import get_settings

    get_settings.cache_clear()
    try:
        runner = CliRunner()
        cli_res = await asyncio.to_thread(
            runner.invoke, cli_app, ["search", "reindex", "--batch-size", "10"]
        )
        assert cli_res.exit_code == 0, cli_res.output
        assert "Reindexed 1 items" in cli_res.output
    finally:
        get_settings.cache_clear()
