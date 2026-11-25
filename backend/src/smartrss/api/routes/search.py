"""PostgreSQL full-text archive search endpoint: GET /api/v1/search (§8.2)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from smartrss.api.deps import get_current_device, get_db_session
from smartrss.api.schemas import (
    SearchItemDTO,
    SearchResponse,
    SearchResultDTO,
    SnippetFragmentDTO,
)
from smartrss.db.models import Device
from smartrss.search.fts import NegationOnlySearchQueryError, search_items

router = APIRouter(prefix="/api/v1/search", tags=["search"])


def _require_aware(dt: datetime | None, name: str) -> datetime | None:
    if dt is not None and (dt.tzinfo is None or dt.utcoffset() is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{name} must be timezone-aware",
        )
    return dt


@router.get("", response_model=SearchResponse)
async def search_endpoint(
    request: Request,
    q: str | None = Query(default=None, max_length=500),
    feed_id: uuid.UUID | None = Query(default=None),
    category_id: uuid.UUID | None = Query(default=None),
    state: Literal["unread", "read", "expired"] | None = Query(default=None),
    is_starred: bool | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    sort: Literal["relevance", "newest"] = Query(default="relevance"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    _device: Device = Depends(get_current_device),
    session: AsyncSession = Depends(get_db_session),
) -> SearchResponse:
    _require_aware(date_from, "date_from")
    _require_aware(date_to, "date_to")

    fts_language: str = request.app.state.settings.fts_language
    try:
        page = await search_items(
            session,
            q=q,
            feed_id=feed_id,
            category_id=category_id,
            state=state,
            is_starred=is_starred,
            date_from=date_from,
            date_to=date_to,
            sort=sort,
            limit=limit,
            offset=offset,
            fts_language=fts_language,
        )
    except NegationOnlySearchQueryError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return SearchResponse(
        query=page.query,
        sort=page.sort,
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        results=[
            SearchResultDTO(
                item=SearchItemDTO(
                    id=r.id,
                    feed_id=r.feed_id,
                    cluster_id=None,
                    raw_url=r.raw_url,
                    canonical_url=r.canonical_url,
                    title=r.title,
                    author=r.author,
                    published_at=r.published_at,
                    fetched_at=r.fetched_at,
                    lead_excerpt=r.lead_excerpt,
                    extraction_status=r.extraction_status,
                    state=r.state,
                    is_starred=r.is_starred,
                    read_at=r.read_at,
                    starred_at=r.starred_at,
                    expired_at=r.expired_at,
                    unexpired_at=r.unexpired_at,
                    read_state_updated_at=r.read_state_updated_at,
                    starred_updated_at=r.starred_updated_at,
                    ingest_sync_seq=r.ingest_sync_seq,
                    enrichment=None,
                    tags=[],
                    research_sources=[],
                    user_feedback=None,
                    updated_at=r.updated_at,
                ),
                rank=r.rank,
                snippet=[
                    [SnippetFragmentDTO(text=frag.text, highlight=frag.highlight) for frag in group]
                    for group in r.snippet
                ],
            )
            for r in page.results
        ],
    )
