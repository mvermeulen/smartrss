"""Full-text article fetch and isolated HTML extraction stage (extract_content, §9.2)."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smartrss.config import get_settings
from smartrss.db.models import Feed, Item
from smartrss.db.sync_log import SyncEvent, execute_synced, sync_log
from smartrss.domain.url_canon import compute_text_hash
from smartrss.extract.client import extract_html_isolated
from smartrss.search.fts import rebuild_item_search_vector
from smartrss.security.safe_http import (
    DomainRateLimiter,
    ResponseTooLargeError,
    SafeHttpClient,
    SafeHttpError,
    SSRFBlockedError,
    TooManyRedirectsError,
    UnsupportedEncodingError,
)

logger = logging.getLogger(__name__)

MAX_ARTICLE_BYTES = 5_000_000
MIN_EXTRACTED_CHARS = 100
HTML_MEDIA_TYPES: frozenset[str] = frozenset({"text/html", "application/xhtml+xml"})
_CHARSET_RE = re.compile(r"charset=([^\s;]+)", re.IGNORECASE)


class TransientExtractionError(RuntimeError):
    """Retryable transient HTTP/network failure during article extraction."""


@dataclass(frozen=True, slots=True)
class ExtractContentOutcome:
    metric_status: str  # "ok" | "skipped"
    extraction_status: str


def _is_html_content_type(content_type: str | None) -> bool:
    if not content_type:
        return True  # Some servers omit Content-Type on HTML pages
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type in HTML_MEDIA_TYPES


def _prepare_html_input(body: bytes, content_type: str | None) -> str | bytes:
    """Decode HTML if HTTP header specifies a valid charset; else pass raw bytes so
    trafilatura can sniff <meta charset> / BOM / encoding directly (I-06)."""
    if content_type:
        match = _CHARSET_RE.search(content_type)
        if match:
            encoding = match.group(1).strip("\"'")
            try:
                return body.decode(encoding, errors="replace")
            except LookupError:
                pass
    return body


async def _set_extraction_status(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    extraction_status: str,
    extracted_text: str | None = None,
    content_hash: str | None = None,
) -> None:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "extraction_status": extraction_status,
        "updated_at": now,
    }
    if extracted_text is not None:
        values["extracted_text"] = extracted_text
    if content_hash is not None:
        values["content_hash"] = content_hash

    _, events = await execute_synced(
        session,
        sa.update(Item).where(Item.id == item_id).values(**values),
        lambda _res: [SyncEvent("item", item_id, "upsert")],
    )
    if extraction_status == "ok" and extracted_text is not None:
        item_meta = (
            await session.execute(
                sa.select(Item.title, Item.author, Item.raw_summary).where(Item.id == item_id)
            )
        ).one_or_none()
        if item_meta is not None:
            await rebuild_item_search_vector(
                session,
                item_id=item_id,
                title=item_meta.title,
                author=item_meta.author,
                raw_summary=item_meta.raw_summary,
                extracted_text=extracted_text,
                fts_language=get_settings().fts_language,
            )
    await sync_log.record(session, events)


async def mark_extract_content_failed(
    session_factory: async_sessionmaker[AsyncSession],
    item_id: uuid.UUID,
) -> None:
    """Final-attempt / reaper dead-letter hook: set extraction_status='failed' (§8.1, §9.2)."""
    async with session_factory() as session:
        exists = (
            await session.execute(sa.select(Item.id).where(Item.id == item_id))
        ).scalar_one_or_none()
        if exists is None:
            return
        await _set_extraction_status(session, item_id=item_id, extraction_status="failed")
        await session.commit()


async def run_extract_content(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: SafeHttpClient,
    rate_limiter: DomainRateLimiter,
    *,
    item_id: uuid.UUID,
) -> ExtractContentOutcome:
    """Execute full-text fetch + isolated HTML extraction for a single item."""
    async with session_factory() as session:
        row = (
            await session.execute(
                sa.select(
                    Item.id,
                    Item.raw_url,
                    Item.title,
                    Feed.fetch_full_text,
                )
                .join(Feed, Feed.id == Item.feed_id)
                .where(Item.id == item_id)
            )
        ).one_or_none()

    if row is None:
        return ExtractContentOutcome(metric_status="skipped", extraction_status="failed")

    if not row.fetch_full_text:
        async with session_factory() as session:
            await _set_extraction_status(session, item_id=item_id, extraction_status="fallback_rss")
            await session.commit()
        return ExtractContentOutcome(metric_status="ok", extraction_status="fallback_rss")

    host = urlsplit(row.raw_url).hostname
    slot = await rate_limiter.acquire_slot(host)
    async with slot:
        try:
            fetch_result = await http_client.fetch(row.raw_url, max_bytes=MAX_ARTICLE_BYTES)
        except (
            SSRFBlockedError,
            UnsupportedEncodingError,
            ResponseTooLargeError,
            TooManyRedirectsError,
        ):
            async with session_factory() as session:
                await _set_extraction_status(
                    session, item_id=item_id, extraction_status="fallback_rss"
                )
                await session.commit()
            return ExtractContentOutcome(metric_status="ok", extraction_status="fallback_rss")
        except (SafeHttpError, TimeoutError, httpx.HTTPError, OSError) as exc:
            raise TransientExtractionError(f"{type(exc).__name__}: {exc}") from exc

    status_code = fetch_result.status_code
    if 400 <= status_code < 500:
        async with session_factory() as session:
            await _set_extraction_status(session, item_id=item_id, extraction_status="fallback_rss")
            await session.commit()
        return ExtractContentOutcome(metric_status="ok", extraction_status="fallback_rss")

    if status_code >= 500 or status_code < 200 or status_code >= 300:
        raise TransientExtractionError(f"HTTP {status_code} fetching {row.raw_url}")

    if not _is_html_content_type(fetch_result.content_type):
        async with session_factory() as session:
            await _set_extraction_status(session, item_id=item_id, extraction_status="fallback_rss")
            await session.commit()
        return ExtractContentOutcome(metric_status="ok", extraction_status="fallback_rss")

    html_input = _prepare_html_input(fetch_result.body_bytes, fetch_result.content_type)
    extracted = await extract_html_isolated(url=fetch_result.final_url, html=html_input)

    async with session_factory() as session:
        if extracted.ok and extracted.text and len(extracted.text.strip()) >= MIN_EXTRACTED_CHARS:
            new_content_hash = compute_text_hash(row.title, extracted.text)
            await _set_extraction_status(
                session,
                item_id=item_id,
                extraction_status="ok",
                extracted_text=extracted.text,
                content_hash=new_content_hash,
            )
            await session.commit()
            return ExtractContentOutcome(metric_status="ok", extraction_status="ok")

        await _set_extraction_status(session, item_id=item_id, extraction_status="fallback_rss")
        await session.commit()
        return ExtractContentOutcome(metric_status="ok", extraction_status="fallback_rss")
