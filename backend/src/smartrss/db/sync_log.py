"""Commit-ordered sync change log writer and structural test guards (ADR 0004)."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, cast

import sqlalchemy as sa
from sqlalchemy import event, inspect
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, SessionTransaction

from smartrss.db.models import AppSettings, Category, Feed, Item, SyncChange, SyncState

APP_SETTINGS_ENTITY_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
INTEREST_PROFILE_ENTITY_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

EntityType = Literal[
    "category",
    "feed",
    "item",
    "cluster",
    "interest_profile",
    "app_settings",
]
SyncOp = Literal["upsert", "delete"]

FEED_SYNCED_COLUMNS: frozenset[str] = frozenset(
    {
        "feed_url",
        "site_url",
        "title",
        "status",
        "health_state",
        "poll_interval_minutes",
        "consecutive_errors",
        "last_error",
        "category_id",
        "expiration_days_override",
        "fetch_full_text",
        "research_policy",
        "curation_instructions",
        "config_version",
        "field_updated_at",
    }
)

ITEM_SYNCED_COLUMNS: frozenset[str] = frozenset(
    {
        "feed_id",
        "guid",
        "raw_url",
        "canonical_url",
        "title",
        "author",
        "published_at",
        "fetched_at",
        "raw_summary",
        "extracted_text",
        "extraction_status",
        "entry_hash",
        "content_hash",
        "state",
        "is_starred",
        "read_at",
        "starred_at",
        "expired_at",
        "read_state_updated_at",
        "starred_updated_at",
        "unexpired_at",
    }
)

APP_SETTINGS_SYNCED_COLUMNS: frozenset[str] = frozenset(
    {
        "default_expiration_days",
        "backfill_cap",
        "default_poll_interval_minutes",
    }
)

SYNC_CHANGE_INSERT_CHUNK_SIZE = 1000

SYNCED_TABLES: frozenset[str] = frozenset({"categories", "feeds", "items", "app_settings"})

_WRITE_TABLE_RE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:ONLY\s+)?(?:\"?public\"?\.)?\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?",
    re.IGNORECASE,
)

_in_synced_core_exec: ContextVar[bool] = ContextVar("smartrss_in_synced_core_exec", default=False)
_in_orm_flush: ContextVar[bool] = ContextVar("smartrss_in_orm_flush", default=False)
_bypass_sync_guard: ContextVar[bool] = ContextVar("smartrss_bypass_sync_guard", default=False)


class MissingSyncEventError(RuntimeError):
    """Raised when a transaction mutates a synced entity without recording a sync_changes event."""


@dataclass(frozen=True, slots=True)
class SyncEvent:
    entity_type: EntityType
    entity_id: uuid.UUID
    op: SyncOp = "upsert"


@contextmanager
def bypass_sync_guard():  # type: ignore[no-untyped-def]
    """Explicit opt-out for test fixture setup or migrations."""
    token = _bypass_sync_guard.set(True)
    try:
        yield
    finally:
        _bypass_sync_guard.reset(token)


class SyncLogWriter:
    """Allocates commit-ordered sync_changes sequence numbers under sync_state FOR UPDATE."""

    async def record(
        self,
        session: AsyncSession,
        events: Sequence[SyncEvent],
    ) -> list[int]:
        """Record events in sync_changes as the final write before commit."""
        if not events:
            return []

        result = await session.execute(
            sa.select(SyncState.current_seq, sa.func.clock_timestamp())
            .where(SyncState.id == 1)
            .with_for_update()
        )
        current_seq_raw, changed_at = result.one()
        current_seq = cast(int, current_seq_raw)
        count = len(events)
        new_seq = current_seq + count
        seqs = list(range(current_seq + 1, new_seq + 1))

        await session.execute(
            sa.update(SyncState)
            .where(SyncState.id == 1)
            .values(current_seq=new_seq, updated_at=changed_at)
        )
        rows = [
            {
                "seq": seq,
                "entity_type": ev.entity_type,
                "entity_id": ev.entity_id,
                "op": ev.op,
                "changed_at": changed_at,
            }
            for seq, ev in zip(seqs, events, strict=True)
        ]
        for start in range(0, len(rows), SYNC_CHANGE_INSERT_CHUNK_SIZE):
            await session.execute(
                sa.insert(SyncChange),
                rows[start : start + SYNC_CHANGE_INSERT_CHUNK_SIZE],
            )

        recorded: set[tuple[str, uuid.UUID]] = session.info.setdefault("synclog_recorded", set())
        for ev in events:
            recorded.add((ev.entity_type, ev.entity_id))

        return seqs


sync_log = SyncLogWriter()


async def execute_synced(
    session: AsyncSession,
    stmt: Any,
    events_fn: Callable[[CursorResult[Any]], Sequence[SyncEvent]],
) -> tuple[CursorResult[Any], list[SyncEvent]]:
    """Execute a Core SQL write on a synced table and track required sync events.

    Callers MUST pass the returned events (along with any other events in the
    same transaction) to ``await sync_log.record(session, events)`` right before
    ``await session.commit()``. For poll-bookkeeping-only writes that intentionally
    emit zero sync events, pass ``lambda _res: []``.
    """
    token = _in_synced_core_exec.set(True)
    try:
        result = cast(CursorResult[Any], await session.execute(stmt))
    finally:
        _in_synced_core_exec.reset(token)

    events = list(events_fn(result))
    if events:
        required: set[tuple[str, uuid.UUID]] = session.info.setdefault(
            "synclog_required_core", set()
        )
        for ev in events:
            required.add((ev.entity_type, ev.entity_id))
    return result, events


def _entity_requires_sync(obj: Any, *, is_new: bool) -> tuple[str, uuid.UUID] | None:
    if isinstance(obj, Category):
        return ("category", obj.id)
    if isinstance(obj, Item):
        if is_new:
            return ("item", obj.id)
        state = inspect(obj)
        for col in ITEM_SYNCED_COLUMNS:
            if state.attrs[col].history.has_changes():
                return ("item", obj.id)
        return None
    if isinstance(obj, Feed):
        if is_new:
            return ("feed", obj.id)
        state = inspect(obj)
        for col in FEED_SYNCED_COLUMNS:
            if state.attrs[col].history.has_changes():
                return ("feed", obj.id)
        return None
    if isinstance(obj, AppSettings):
        if is_new:
            return ("app_settings", APP_SETTINGS_ENTITY_ID)
        state = inspect(obj)
        for col in APP_SETTINGS_SYNCED_COLUMNS:
            if state.attrs[col].history.has_changes():
                return ("app_settings", APP_SETTINGS_ENTITY_ID)
        return None
    return None


def _collect_orm_required(session: Session) -> None:
    if _bypass_sync_guard.get():
        return
    required: set[tuple[str, uuid.UUID]] = session.info.setdefault("synclog_required_orm", set())
    for obj in session.new:
        req = _entity_requires_sync(obj, is_new=True)
        if req is not None:
            required.add(req)
    for obj in session.dirty:
        if not session.is_modified(obj, include_collections=False):
            continue
        req = _entity_requires_sync(obj, is_new=False)
        if req is not None:
            required.add(req)
    for obj in session.deleted:
        req = _entity_requires_sync(obj, is_new=True)
        if req is not None:
            required.add(req)


def _before_flush(session: Session, flush_context: Any, instances: Any) -> None:
    _collect_orm_required(session)
    token = _in_orm_flush.set(True)
    tokens: list[Any] = session.info.setdefault("_orm_flush_tokens", [])
    tokens.append(token)


def _after_flush_postexec(session: Session, flush_context: Any) -> None:
    tokens: list[Any] = session.info.get("_orm_flush_tokens", [])
    if tokens:
        _in_orm_flush.reset(tokens.pop())


def _before_commit(session: Session) -> None:
    if _bypass_sync_guard.get():
        return
    _collect_orm_required(session)
    required: set[tuple[str, uuid.UUID]] = set()
    required.update(session.info.get("synclog_required_orm", set()))
    required.update(session.info.get("synclog_required_core", set()))
    if not required:
        return
    recorded: set[tuple[str, uuid.UUID]] = session.info.get("synclog_recorded", set())
    missing = required - recorded
    if missing:
        formatted = ", ".join(f"{etype}:{eid}" for etype, eid in sorted(missing, key=str))
        raise MissingSyncEventError(
            f"Transaction mutated synced entity(ies) without SyncLogWriter.record(): {formatted}"
        )


def _clear_tracking(session: Session, *_args: Any) -> None:
    session.info.pop("synclog_recorded", None)
    session.info.pop("synclog_required_orm", None)
    session.info.pop("synclog_required_core", None)
    tokens: list[Any] = session.info.pop("_orm_flush_tokens", [])
    while tokens:
        _in_orm_flush.reset(tokens.pop())


def _after_transaction_end(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None:
        _clear_tracking(session)


def _before_cursor_execute(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    if _bypass_sync_guard.get() or _in_synced_core_exec.get() or _in_orm_flush.get():
        return
    for match in _WRITE_TABLE_RE.finditer(statement):
        table = match.group(1).lower()
        if table in SYNCED_TABLES:
            raise MissingSyncEventError(
                f"Direct Core SQL write to synced table '{table}' outside execute_synced(): "
                f"{statement[:160]}"
            )


def install_sync_guards(
    engine: AsyncEngine | Engine,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Install both ORM before_commit and Core before_cursor_execute strict sync guards."""
    sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
    if not event.contains(sync_engine, "before_cursor_execute", _before_cursor_execute):
        event.listen(sync_engine, "before_cursor_execute", _before_cursor_execute)

    if session_factory is not None:
        sync_session_cls = session_factory.class_
        # Attach to the underlying sync Session class used by AsyncSession
        target = getattr(sync_session_cls, "sync_session_class", Session)
        if not event.contains(target, "before_flush", _before_flush):
            event.listen(target, "before_flush", _before_flush)
        if not event.contains(target, "after_flush_postexec", _after_flush_postexec):
            event.listen(target, "after_flush_postexec", _after_flush_postexec)
        if not event.contains(target, "before_commit", _before_commit):
            event.listen(target, "before_commit", _before_commit)
        if not event.contains(target, "after_transaction_end", _after_transaction_end):
            event.listen(target, "after_transaction_end", _after_transaction_end)


def remove_sync_guards(
    engine: AsyncEngine | Engine,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Remove strict sync guards (useful for isolated tests)."""
    sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
    if event.contains(sync_engine, "before_cursor_execute", _before_cursor_execute):
        event.remove(sync_engine, "before_cursor_execute", _before_cursor_execute)

    if session_factory is not None:
        sync_session_cls = session_factory.class_
        target = getattr(sync_session_cls, "sync_session_class", Session)
        for evt, fn in (
            ("before_flush", _before_flush),
            ("after_flush_postexec", _after_flush_postexec),
            ("before_commit", _before_commit),
            ("after_transaction_end", _after_transaction_end),
        ):
            if event.contains(target, evt, fn):
                event.remove(target, evt, fn)
