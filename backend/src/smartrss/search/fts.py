"""PostgreSQL full-text archive search, search_vector maintenance, and reindex (§8)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smartrss.db.models import Feed, Item
from smartrss.db.sync_log import execute_synced
from smartrss.domain.fts_text import (
    MAX_FTS_BODY_CHARS,
    SnippetFragment,
    parse_headline_fragments,
    prepare_headline_input,
    strip_html_to_plain_text,
    validate_fts_language,
)
from smartrss.domain.sync_merge import derive_lead_excerpt

SEARCH_VECTOR_BATCH_SIZE = 500


class NegationOnlySearchQueryError(ValueError):
    """Raised when a search query has no positive required lexeme (querytree = 'T'; P-13)."""


@dataclass(frozen=True, slots=True)
class SearchVectorInput:
    """Fields required to rebuild items.search_vector for one item."""

    item_id: uuid.UUID
    title: str
    author: str | None
    raw_summary: str | None
    extracted_text: str | None


@dataclass(frozen=True, slots=True)
class SearchResultRow:
    """Single full-text search hit with slim item fields, rank, and structured snippets."""

    id: uuid.UUID
    feed_id: uuid.UUID
    raw_url: str
    canonical_url: str
    title: str
    author: str | None
    published_at: datetime | None
    fetched_at: datetime
    lead_excerpt: str | None
    extraction_status: str
    state: str
    is_starred: bool
    read_at: datetime | None
    starred_at: datetime | None
    expired_at: datetime | None
    unexpired_at: datetime | None
    read_state_updated_at: datetime
    starred_updated_at: datetime
    ingest_sync_seq: int
    updated_at: datetime
    rank: float | None
    snippet: list[list[SnippetFragment]]


@dataclass(frozen=True, slots=True)
class SearchPageResult:
    """Paginated archive search result returned by search_items()."""

    query: str | None
    sort: Literal["relevance", "newest"]
    total: int
    limit: int
    offset: int
    results: list[SearchResultRow]


_UPDATE_SEARCH_VECTOR_SQL = sa.text(
    """
    UPDATE items
    SET search_vector =
        setweight(to_tsvector(CAST(:fts_cfg AS regconfig), COALESCE(:title, '')), 'A') ||
        setweight(to_tsvector(CAST(:fts_cfg AS regconfig), COALESCE(:author, '')), 'B') ||
        setweight(to_tsvector(CAST(:fts_cfg AS regconfig), COALESCE(:summary_plain, '')), 'C') ||
        setweight(to_tsvector(CAST(:fts_cfg AS regconfig), COALESCE(:body_slice, '')), 'D')
    WHERE id = :item_id
    """
)


def _build_vector_params(entry: SearchVectorInput, fts_cfg: str) -> dict[str, Any]:
    return {
        "fts_cfg": fts_cfg,
        "item_id": entry.item_id,
        "title": entry.title or "",
        "author": entry.author or "",
        "summary_plain": strip_html_to_plain_text(entry.raw_summary),
        "body_slice": (entry.extracted_text or "")[:MAX_FTS_BODY_CHARS],
    }


async def rebuild_item_search_vector(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    title: str,
    author: str | None,
    raw_summary: str | None,
    extracted_text: str | None,
    fts_language: str = "english",
) -> None:
    """Rebuild items.search_vector for a single item without emitting a sync_changes event."""
    fts_cfg = validate_fts_language(fts_language)
    params = _build_vector_params(
        SearchVectorInput(
            item_id=item_id,
            title=title,
            author=author,
            raw_summary=raw_summary,
            extracted_text=extracted_text,
        ),
        fts_cfg,
    )
    await execute_synced(session, _UPDATE_SEARCH_VECTOR_SQL.bindparams(**params), lambda _res: [])


async def rebuild_items_search_vectors_batch(
    session: AsyncSession,
    entries: Sequence[SearchVectorInput],
    *,
    fts_language: str = "english",
) -> None:
    """Rebuild items.search_vector for a batch of items inside the caller's transaction."""
    if not entries:
        return
    fts_cfg = validate_fts_language(fts_language)
    for entry in entries:
        params = _build_vector_params(entry, fts_cfg)
        await execute_synced(
            session,
            _UPDATE_SEARCH_VECTOR_SQL.bindparams(**params),
            lambda _res: [],
        )


async def reindex_all_items(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    fts_language: str = "english",
    batch_size: int = SEARCH_VECTOR_BATCH_SIZE,
) -> int:
    """Rebuild items.search_vector across the full archive in committed 500-row batches."""
    fts_cfg = validate_fts_language(fts_language)
    total_reindexed = 0
    last_id: uuid.UUID | None = None

    while True:
        async with session_factory() as session:
            stmt = (
                sa.select(
                    Item.id,
                    Item.title,
                    Item.author,
                    Item.raw_summary,
                    sa.func.left(Item.extracted_text, MAX_FTS_BODY_CHARS).label("body_slice"),
                )
                .order_by(Item.id.asc())
                .limit(batch_size)
            )
            if last_id is not None:
                stmt = stmt.where(Item.id > last_id)

            rows = (await session.execute(stmt)).all()
            if not rows:
                break

            entries = [
                SearchVectorInput(
                    item_id=r.id,
                    title=r.title,
                    author=r.author,
                    raw_summary=r.raw_summary,
                    extracted_text=r.body_slice,
                )
                for r in rows
            ]
            await rebuild_items_search_vectors_batch(session, entries, fts_language=fts_cfg)
            await session.commit()

            total_reindexed += len(rows)
            last_id = rows[-1].id

    return total_reindexed


async def search_items(
    session: AsyncSession,
    *,
    q: str | None = None,
    feed_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    state: Literal["unread", "read", "expired"] | None = None,
    is_starred: bool | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    sort: Literal["relevance", "newest"] = "relevance",
    limit: int = 20,
    offset: int = 0,
    fts_language: str = "english",
) -> SearchPageResult:
    """Execute full-archive PostgreSQL FTS with weighted ranking and XSS-safe snippets (§8.2)."""
    fts_cfg = validate_fts_language(fts_language)
    cleaned_q = q.strip() if q is not None and q.strip() else None
    effective_sort: Literal["relevance", "newest"] = "newest" if cleaned_q is None else sort

    if cleaned_q is not None:
        inspect_row = (
            await session.execute(
                sa.text(
                    """
                    WITH parsed AS (
                        SELECT websearch_to_tsquery(CAST(:fts_cfg AS regconfig), :q) AS tsq
                    )
                    SELECT numnode(tsq) AS nn, querytree(tsq) AS qt FROM parsed
                    """
                ),
                {"fts_cfg": fts_cfg, "q": cleaned_q},
            )
        ).one()
        if int(inspect_row.nn) == 0:
            return SearchPageResult(
                query=cleaned_q,
                sort=effective_sort,
                total=0,
                limit=limit,
                offset=offset,
                results=[],
            )
        if inspect_row.qt == "T":
            raise NegationOnlySearchQueryError(
                "Search query must include at least one positive required search term"
            )

    conditions: list[Any] = []
    if feed_id is not None:
        conditions.append(Item.feed_id == feed_id)
    if category_id is not None:
        conditions.append(Feed.category_id == category_id)
    if state is not None:
        conditions.append(Item.state == state)
    if is_starred is not None:
        conditions.append(Item.is_starred.is_(is_starred))
    effective_date_col = sa.func.coalesce(Item.published_at, Item.fetched_at)
    if date_from is not None:
        conditions.append(effective_date_col >= date_from)
    if date_to is not None:
        conditions.append(effective_date_col <= date_to)

    tsq_expr: Any = None
    regconfig_expr = sa.text("CAST(:fts_cfg AS regconfig)")
    if cleaned_q is not None:
        tsq_expr = sa.func.websearch_to_tsquery(regconfig_expr, cleaned_q)
        conditions.append(Item.search_vector.op("@@")(tsq_expr))

    count_stmt = sa.select(sa.func.count(Item.id))
    if category_id is not None:
        count_stmt = count_stmt.join(Feed, Feed.id == Item.feed_id)
    if conditions:
        count_stmt = count_stmt.where(*conditions)

    query_params: dict[str, Any] = {"fts_cfg": fts_cfg} if cleaned_q is not None else {}
    total = int((await session.execute(count_stmt, query_params)).scalar_one())
    if total == 0:
        return SearchPageResult(
            query=cleaned_q,
            sort=effective_sort,
            total=0,
            limit=limit,
            offset=offset,
            results=[],
        )

    rank_col: Any = (
        sa.func.ts_rank_cd(Item.search_vector, tsq_expr)
        if cleaned_q is not None
        else sa.literal(None)
    )

    page_stmt = sa.select(
        Item.id,
        Item.feed_id,
        Item.raw_url,
        Item.canonical_url,
        Item.title,
        Item.author,
        Item.published_at,
        Item.fetched_at,
        sa.func.left(Item.raw_summary, MAX_FTS_BODY_CHARS).label("raw_summary_slice"),
        sa.func.left(Item.extracted_text, MAX_FTS_BODY_CHARS).label("extracted_text_slice"),
        Item.extraction_status,
        Item.state,
        Item.is_starred,
        Item.read_at,
        Item.starred_at,
        Item.expired_at,
        Item.unexpired_at,
        Item.read_state_updated_at,
        Item.starred_updated_at,
        Item.ingest_sync_seq,
        Item.updated_at,
        rank_col.label("rank"),
    )
    if category_id is not None:
        page_stmt = page_stmt.join(Feed, Feed.id == Item.feed_id)
    if conditions:
        page_stmt = page_stmt.where(*conditions)

    if effective_sort == "relevance" and cleaned_q is not None:
        page_stmt = page_stmt.order_by(
            rank_col.desc(),
            effective_date_col.desc(),
            Item.id.desc(),
        )
    else:
        page_stmt = page_stmt.order_by(effective_date_col.desc(), Item.id.desc())

    page_rows = (await session.execute(page_stmt.limit(limit).offset(offset), query_params)).all()

    headline_sql = sa.text(
        """
        SELECT ts_headline(
            CAST(:fts_cfg AS regconfig),
            :plain_text,
            websearch_to_tsquery(CAST(:fts_cfg AS regconfig), :q),
            'StartSel=' || chr(2) || ', StopSel=' || chr(3)
            || ', MaxWords=35, MinWords=15, MaxFragments=2, FragmentDelimiter="' || chr(4) || '"'
        )
        """
    )

    results: list[SearchResultRow] = []
    for row in page_rows:
        snippet: list[list[SnippetFragment]] = []
        if cleaned_q is not None:
            plain_source = prepare_headline_input(
                title=row.title,
                raw_summary=row.raw_summary_slice,
                extracted_text=row.extracted_text_slice,
            )
            hl_raw: str | None = (
                await session.execute(
                    headline_sql,
                    {"fts_cfg": fts_cfg, "plain_text": plain_source, "q": cleaned_q},
                )
            ).scalar_one()
            snippet = parse_headline_fragments(str(hl_raw or ""))

        results.append(
            SearchResultRow(
                id=row.id,
                feed_id=row.feed_id,
                raw_url=row.raw_url,
                canonical_url=row.canonical_url,
                title=row.title,
                author=row.author,
                published_at=row.published_at,
                fetched_at=row.fetched_at,
                lead_excerpt=derive_lead_excerpt(
                    extracted_text=row.extracted_text_slice,
                    raw_summary=row.raw_summary_slice,
                ),
                extraction_status=row.extraction_status,
                state=row.state,
                is_starred=row.is_starred,
                read_at=row.read_at,
                starred_at=row.starred_at,
                expired_at=row.expired_at,
                unexpired_at=row.unexpired_at,
                read_state_updated_at=row.read_state_updated_at,
                starred_updated_at=row.starred_updated_at,
                ingest_sync_seq=row.ingest_sync_seq,
                updated_at=row.updated_at,
                rank=float(row.rank) if row.rank is not None else None,
                snippet=snippet,
            )
        )

    return SearchPageResult(
        query=cleaned_q,
        sort=effective_sort,
        total=total,
        limit=limit,
        offset=offset,
        results=results,
    )
