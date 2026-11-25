"""Server synchronization service: bootstrap, pull coalescing, and single-tx push engine (§6)."""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal, cast

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from smartrss.db.models import (
    AppSettings,
    Category,
    Feed,
    Item,
    SyncChange,
    SyncMutation,
    SyncState,
)
from smartrss.db.sync_log import SyncEvent, execute_synced, sync_log
from smartrss.domain.sync_merge import (
    FEED_STATUS_RANK,
    MAX_RAW_SUMMARY_CHARS,
    ItemStarSnapshot,
    ItemStateSnapshot,
    apply_feed_status_transition,
    cap_raw_summary,
    clamp_client_timestamp,
    compute_payload_sha256,
    derive_lead_excerpt,
    merge_feed_field,
    merge_item_starred,
    merge_item_state,
    parse_field_timestamp,
)
from smartrss.jobs.queue import enqueue_job
from smartrss.sync.schemas import (
    FeedSetFieldMutation,
    ItemMarkBulkReadMutation,
    ItemSetStarredMutation,
    ItemSetStateMutation,
    ReservedFutureMutation,
    SyncAppSettingsDTO,
    SyncBootstrapResponse,
    SyncCategoryDTO,
    SyncChangeData,
    SyncFeedDTO,
    SyncItemDTO,
    SyncMutationInput,
    SyncPullChange,
    SyncPullResponse,
    SyncPushFeedSnapshot,
    SyncPushItemSnapshot,
    SyncPushRequest,
    SyncPushResponse,
    SyncPushResult,
)

HYDRATION_CHUNK_SIZE = 500
LOCK_CHUNK_SIZE = 2000
EXTRACTED_EXCERPT_SQL_SLICE = 8192
MAX_PUSH_RETRIES = 3
RETRY_BACKOFF_MIN_S = 0.02
RETRY_BACKOFF_MAX_S = 0.10


class PushTransientError(RuntimeError):
    """Raised when POST /api/v1/sync/push exhausts transient DB retry attempts (503)."""


def _item_hydration_columns() -> tuple[Any, ...]:
    return (
        Item.id,
        Item.feed_id,
        Item.raw_url,
        Item.canonical_url,
        Item.title,
        Item.author,
        Item.published_at,
        Item.fetched_at,
        sa.func.left(Item.raw_summary, MAX_RAW_SUMMARY_CHARS + 1).label("raw_summary_slice"),
        sa.func.left(Item.extracted_text, EXTRACTED_EXCERPT_SQL_SLICE).label(
            "extracted_text_slice"
        ),
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
    )


def _row_to_sync_item_dto(row: Any) -> SyncItemDTO:
    capped_summary, truncated = cap_raw_summary(row.raw_summary_slice)
    lead_excerpt = derive_lead_excerpt(
        extracted_text=row.extracted_text_slice,
        raw_summary=row.raw_summary_slice,
    )
    return SyncItemDTO(
        id=row.id,
        feed_id=row.feed_id,
        cluster_id=None,
        raw_url=row.raw_url,
        canonical_url=row.canonical_url,
        title=row.title,
        author=row.author,
        published_at=row.published_at,
        fetched_at=row.fetched_at,
        raw_summary=capped_summary,
        summary_truncated=truncated,
        lead_excerpt=lead_excerpt,
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
        enrichment=None,
        tags=[],
        research_sources=[],
        user_feedback=None,
        updated_at=row.updated_at,
    )


def _feed_to_sync_dto(feed: Feed) -> SyncFeedDTO:
    return SyncFeedDTO(
        id=feed.id,
        category_id=feed.category_id,
        feed_url=feed.feed_url,
        site_url=feed.site_url,
        title=feed.title,
        status=feed.status,
        health_state=feed.health_state,
        poll_interval_minutes=feed.poll_interval_minutes,
        consecutive_errors=feed.consecutive_errors,
        last_error=feed.last_error,
        expiration_days_override=feed.expiration_days_override,
        fetch_full_text=feed.fetch_full_text,
        research_policy=dict(feed.research_policy or {}),
        curation_instructions=feed.curation_instructions,
        active_feed_instructions=[],
        config_version=feed.config_version,
        field_updated_at=dict(feed.field_updated_at or {}),
        updated_at=feed.updated_at,
    )


async def _begin_repeatable_read_ro(session: AsyncSession) -> None:
    """Start a fresh REPEATABLE READ READ ONLY transaction (clearing any prior auth read tx)."""
    if session.in_transaction():
        await session.rollback()
    await session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))


async def sync_bootstrap(
    session: AsyncSession,
    *,
    cursor: uuid.UUID | None,
    limit: int,
) -> SyncBootstrapResponse:
    """Execute stateless keyset-paginated bootstrap pinned to Page 1's watermark (§6.2)."""
    await _begin_repeatable_read_ro(session)

    server_now: datetime = (await session.execute(sa.select(sa.func.now()))).scalar_one()
    sync_state = (await session.execute(sa.select(SyncState).where(SyncState.id == 1))).scalar_one()

    working_set_filter = sa.or_(
        Item.state == "unread",
        Item.is_starred.is_(True),
        sa.and_(
            Item.state == "read",
            Item.read_at >= sa.func.now() - sa.text("INTERVAL '14 days'"),
        ),
    )
    item_stmt = sa.select(*_item_hydration_columns()).where(working_set_filter)
    if cursor is not None:
        item_stmt = item_stmt.where(Item.id > cursor)
    item_stmt = item_stmt.order_by(Item.id.asc()).limit(limit + 1)

    raw_items = (await session.execute(item_stmt)).all()
    has_more = len(raw_items) > limit
    page_items = raw_items[:limit]
    next_cursor = page_items[-1].id if has_more else None
    item_dtos = [_row_to_sync_item_dto(r) for r in page_items]

    if cursor is not None:
        return SyncBootstrapResponse(
            instance_epoch=sync_state.instance_epoch,
            server_now=server_now,
            watermark=None,
            categories=None,
            feeds=None,
            clusters=None,
            interest_profile=None,
            app_settings=None,
            items=item_dtos,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    cat_rows = (
        (
            await session.execute(
                sa.select(Category)
                .where(Category.deleted_at.is_(None))
                .order_by(Category.sort_order.asc(), Category.id.asc())
            )
        )
        .scalars()
        .all()
    )
    feed_rows = (await session.execute(sa.select(Feed).order_by(Feed.id.asc()))).scalars().all()
    settings_row = (
        await session.execute(sa.select(AppSettings).where(AppSettings.id == 1))
    ).scalar_one()

    return SyncBootstrapResponse(
        instance_epoch=sync_state.instance_epoch,
        server_now=server_now,
        watermark=sync_state.current_seq,
        categories=[SyncCategoryDTO.model_validate(c) for c in cat_rows],
        feeds=[_feed_to_sync_dto(f) for f in feed_rows],
        clusters=[],
        interest_profile=None,
        app_settings=SyncAppSettingsDTO.model_validate(settings_row),
        items=item_dtos,
        next_cursor=next_cursor,
        has_more=has_more,
    )


async def sync_pull(
    session: AsyncSession,
    *,
    since_seq: int,
    epoch: uuid.UUID,
    limit: int,
) -> SyncPullResponse:
    """Execute commit-ordered coalesced delta pull inside one REPEATABLE READ snapshot (§6.3)."""
    await _begin_repeatable_read_ro(session)

    server_now: datetime = (await session.execute(sa.select(sa.func.now()))).scalar_one()
    sync_state = (await session.execute(sa.select(SyncState).where(SyncState.id == 1))).scalar_one()

    resync_reason: Literal["epoch_mismatch", "cursor_ahead", "cursor_pruned"] | None = None
    if epoch != sync_state.instance_epoch:
        resync_reason = "epoch_mismatch"
    elif since_seq > sync_state.current_seq:
        resync_reason = "cursor_ahead"
    elif since_seq < sync_state.pruned_through_seq:
        resync_reason = "cursor_pruned"

    if resync_reason is not None:
        return SyncPullResponse(
            resync_required=True,
            resync_reason=resync_reason,
            instance_epoch=sync_state.instance_epoch,
            server_now=server_now,
            watermark=sync_state.current_seq,
            next_seq=sync_state.current_seq,
            has_more=False,
            changes=[],
        )

    raw_rows = (
        await session.execute(
            sa.select(
                SyncChange.seq,
                SyncChange.entity_type,
                SyncChange.entity_id,
                SyncChange.op,
                SyncChange.changed_at,
            )
            .where(
                SyncChange.seq > since_seq,
                SyncChange.seq <= sync_state.current_seq,
            )
            .order_by(SyncChange.seq.asc())
            .limit(limit + 1)
        )
    ).all()

    has_more = len(raw_rows) > limit
    page_rows = raw_rows[:limit]
    if not page_rows:
        return SyncPullResponse(
            resync_required=False,
            resync_reason=None,
            instance_epoch=sync_state.instance_epoch,
            server_now=server_now,
            watermark=sync_state.current_seq,
            next_seq=since_seq,
            has_more=False,
            changes=[],
        )

    next_seq = int(page_rows[-1].seq)

    # Within-page coalescing: keep latest seq per (entity_type, entity_id), ordered by seq ASC
    latest_by_key: dict[tuple[str, uuid.UUID], Any] = {}
    for row in page_rows:
        latest_by_key[(row.entity_type, row.entity_id)] = row
    coalesced_rows = sorted(latest_by_key.values(), key=lambda r: int(r.seq))

    # Group upsert IDs by entity_type for chunked bulk hydration
    upsert_ids: dict[str, list[uuid.UUID]] = {}
    for row in coalesced_rows:
        if row.op == "upsert":
            upsert_ids.setdefault(row.entity_type, []).append(row.entity_id)

    categories_map: dict[uuid.UUID, SyncCategoryDTO] = {}
    for start in range(0, len(upsert_ids.get("category", [])), HYDRATION_CHUNK_SIZE):
        chunk = upsert_ids["category"][start : start + HYDRATION_CHUNK_SIZE]
        cat_rows = (
            (await session.execute(sa.select(Category).where(Category.id.in_(chunk))))
            .scalars()
            .all()
        )
        for c in cat_rows:
            if c.deleted_at is None:
                categories_map[c.id] = SyncCategoryDTO.model_validate(c)

    feeds_map: dict[uuid.UUID, SyncFeedDTO] = {}
    for start in range(0, len(upsert_ids.get("feed", [])), HYDRATION_CHUNK_SIZE):
        chunk = upsert_ids["feed"][start : start + HYDRATION_CHUNK_SIZE]
        feed_rows = (
            (await session.execute(sa.select(Feed).where(Feed.id.in_(chunk)))).scalars().all()
        )
        for f in feed_rows:
            feeds_map[f.id] = _feed_to_sync_dto(f)

    items_map: dict[uuid.UUID, SyncItemDTO] = {}
    for start in range(0, len(upsert_ids.get("item", [])), HYDRATION_CHUNK_SIZE):
        chunk = upsert_ids["item"][start : start + HYDRATION_CHUNK_SIZE]
        item_rows = (
            await session.execute(sa.select(*_item_hydration_columns()).where(Item.id.in_(chunk)))
        ).all()
        for r in item_rows:
            items_map[r.id] = _row_to_sync_item_dto(r)

    app_settings_dto: SyncAppSettingsDTO | None = None
    if upsert_ids.get("app_settings"):
        settings_row = (
            await session.execute(sa.select(AppSettings).where(AppSettings.id == 1))
        ).scalar_one_or_none()
        if settings_row is not None:
            app_settings_dto = SyncAppSettingsDTO.model_validate(settings_row)

    changes: list[SyncPullChange] = []
    for row in coalesced_rows:
        op: Literal["upsert", "delete"] = "delete" if row.op == "delete" else "upsert"
        data: SyncChangeData = None
        if op == "upsert":
            etype = row.entity_type
            eid = row.entity_id
            if etype == "category":
                data = categories_map.get(eid)
            elif etype == "feed":
                data = feeds_map.get(eid)
            elif etype == "item":
                data = items_map.get(eid)
            elif etype == "app_settings":
                data = app_settings_dto
            if data is None:
                op = "delete"

        changes.append(
            SyncPullChange(
                seq=int(row.seq),
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                op=op,
                changed_at=row.changed_at,
                data=data,
            )
        )

    return SyncPullResponse(
        resync_required=False,
        resync_reason=None,
        instance_epoch=sync_state.instance_epoch,
        server_now=server_now,
        watermark=sync_state.current_seq,
        next_seq=next_seq,
        has_more=has_more,
        changes=changes,
    )


def _mutation_payload_sha256(mutation: SyncMutationInput) -> str:
    payload = mutation.model_dump(mode="json", exclude={"client_mutation_id"})
    return compute_payload_sha256(payload)


def _mutation_entity_meta(mutation: SyncMutationInput) -> tuple[str, uuid.UUID | None]:
    if isinstance(mutation, (ItemSetStateMutation, ItemSetStarredMutation)):
        return "item", mutation.item_id
    if isinstance(mutation, FeedSetFieldMutation):
        return "feed", mutation.feed_id
    if isinstance(mutation, ItemMarkBulkReadMutation):
        if mutation.scope == "feed":
            return "bulk", mutation.feed_id
        if mutation.scope == "category":
            return "bulk", mutation.category_id
        return "bulk", None
    return "cluster" if mutation.type == "split_cluster" else "bulk", None


def _validate_feed_field_value(
    field: str,
    value: Any,
    active_category_ids: set[uuid.UUID],
) -> tuple[bool, Any, str | None]:
    """Validate feed_set_field value; returns (ok, coerced_value, rejection_reason)."""
    if field == "status":
        if not isinstance(value, str) or value not in FEED_STATUS_RANK:
            return False, None, "invalid_field_value"
        return True, value, None

    if field == "category_id":
        if value is None:
            return True, None, None
        try:
            parsed = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        except (ValueError, TypeError):
            return False, None, "invalid_field_value"
        if parsed not in active_category_ids:
            return False, None, "category_not_found"
        return True, parsed, None

    if field == "expiration_days_override":
        if value is None:
            return True, None, None
        if isinstance(value, bool) or not isinstance(value, int) or not (0 <= value <= 3650):
            return False, None, "invalid_field_value"
        return True, value, None

    if field in ("research_policy.enabled", "research_policy.fetch_pdfs"):
        if not isinstance(value, bool):
            return False, None, "invalid_field_value"
        return True, value, None

    return False, None, "invalid_field_value"


def _extract_sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    if orig is None:
        return None
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return str(code) if code is not None else None


def _is_retryable_push_error(exc: BaseException) -> bool:
    code = _extract_sqlstate(exc)
    if code in ("40P01", "40001"):
        return True
    if code == "23505":
        text = f"{exc} {getattr(exc, 'orig', '')}".lower()
        return "sync_mutations" in text
    return False


def _scoped_bulk_conditions(
    mutation: ItemMarkBulkReadMutation,
    *,
    effective_ts: datetime | None = None,
) -> list[Any]:
    conds: list[Any] = [
        Item.state == "unread",
        Item.ingest_sync_seq <= mutation.up_to_sync_seq,
    ]
    if mutation.scope == "feed":
        conds.append(Item.feed_id == mutation.feed_id)
    elif mutation.scope == "category":
        feed_subq = sa.select(Feed.id).where(Feed.category_id == mutation.category_id)
        conds.append(Item.feed_id.in_(feed_subq))
    if mutation.older_than_ts is not None:
        conds.append(sa.func.coalesce(Item.published_at, Item.fetched_at) < mutation.older_than_ts)
    if effective_ts is not None:
        conds.append(Item.read_state_updated_at <= effective_ts)
    return conds


async def _build_replay_snapshot(
    session: AsyncSession,
    existing: SyncMutation,
    *,
    items_by_id: dict[uuid.UUID, Item],
    feeds_by_id: dict[uuid.UUID, Feed],
) -> tuple[Literal["item", "feed"] | None, SyncPushItemSnapshot | SyncPushFeedSnapshot | None]:
    if existing.outcome == "applied" or existing.entity_id is None:
        return None, None
    if existing.entity_type == "item":
        item = items_by_id.get(existing.entity_id)
        if item is None:
            item = await session.get(Item, existing.entity_id)
        if item is not None:
            return "item", SyncPushItemSnapshot.model_validate(item)
    elif existing.entity_type == "feed":
        feed = feeds_by_id.get(existing.entity_id)
        if feed is None:
            feed = await session.get(Feed, existing.entity_id)
        if feed is not None:
            return "feed", SyncPushFeedSnapshot.model_validate(feed)
    return None, None


async def _execute_push_once(
    session: AsyncSession,
    *,
    device_id: uuid.UUID,
    request: SyncPushRequest,
) -> SyncPushResponse:
    if session.in_transaction():
        await session.rollback()

    server_now: datetime = (await session.execute(sa.select(sa.func.now()))).scalar_one()
    current_epoch: uuid.UUID = (
        await session.execute(sa.select(SyncState.instance_epoch).where(SyncState.id == 1))
    ).scalar_one()

    mutations = request.mutations
    if not mutations:
        return SyncPushResponse(
            instance_epoch=current_epoch,
            server_now=server_now,
            results=[],
        )

    # Step 2: compute payload hashes and bulk-load existing sync_mutations
    payload_hashes: list[str] = [_mutation_payload_sha256(m) for m in mutations]
    unique_ids = list({m.client_mutation_id for m in mutations})
    existing_mutations: dict[uuid.UUID, SyncMutation] = {}
    for start in range(0, len(unique_ids), HYDRATION_CHUNK_SIZE):
        chunk = unique_ids[start : start + HYDRATION_CHUNK_SIZE]
        rows = (
            (
                await session.execute(
                    sa.select(SyncMutation).where(SyncMutation.client_mutation_id.in_(chunk))
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            existing_mutations[row.client_mutation_id] = row

    # Identify first-seen non-persisted mutations so Step 3 locks only needed targets
    seen_ids_for_plan: set[uuid.UUID] = set()
    to_execute_indices: set[int] = set()
    for idx, m in enumerate(mutations):
        mid = m.client_mutation_id
        if mid in existing_mutations or mid in seen_ids_for_plan:
            continue
        seen_ids_for_plan.add(mid)
        to_execute_indices.add(idx)

    # Step 3: Collect target IDs and acquire row locks in strict §5.1 hierarchy:
    # categories (id ASC) -> feeds (id ASC) -> items (id ASC) -> jobs -> sync_state
    cat_ids_to_check: set[uuid.UUID] = set()
    feed_ids_to_lock: set[uuid.UUID] = set()
    explicit_item_ids: set[uuid.UUID] = set()
    scoped_bulk_indices: list[int] = []

    for idx in to_execute_indices:
        m = mutations[idx]
        if isinstance(m, (ItemSetStateMutation, ItemSetStarredMutation)):
            explicit_item_ids.add(m.item_id)
        elif isinstance(m, FeedSetFieldMutation):
            feed_ids_to_lock.add(m.feed_id)
            if m.field == "category_id" and m.value is not None:
                try:
                    cid = m.value if isinstance(m.value, uuid.UUID) else uuid.UUID(str(m.value))
                    cat_ids_to_check.add(cid)
                except (ValueError, TypeError):
                    pass
        elif isinstance(m, ItemMarkBulkReadMutation):
            if m.scope == "items" and m.item_ids is not None:
                explicit_item_ids.update(m.item_ids)
            elif m.epoch == current_epoch:
                if m.scope == "feed" and m.feed_id is not None:
                    feed_ids_to_lock.add(m.feed_id)
                elif m.scope == "category" and m.category_id is not None:
                    cat_ids_to_check.add(m.category_id)
                eff_preview, skew_err = clamp_client_timestamp(m.client_timestamp, server_now)
                if skew_err is None and eff_preview is not None:
                    scoped_bulk_indices.append(idx)

    # 3a. Active categories (id ASC)
    active_category_ids: set[uuid.UUID] = set()
    if cat_ids_to_check:
        sorted_cats = sorted(cat_ids_to_check)
        for start in range(0, len(sorted_cats), HYDRATION_CHUNK_SIZE):
            chunk = sorted_cats[start : start + HYDRATION_CHUNK_SIZE]
            cat_id_rows = (
                (
                    await session.execute(
                        sa.select(Category.id)
                        .where(Category.id.in_(chunk), Category.deleted_at.is_(None))
                        .order_by(Category.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            active_category_ids.update(cat_id_rows)

    # 3b. Feeds (id ASC FOR UPDATE)
    feeds_by_id: dict[uuid.UUID, Feed] = {}
    if feed_ids_to_lock:
        sorted_feeds = sorted(feed_ids_to_lock)
        for start in range(0, len(sorted_feeds), HYDRATION_CHUNK_SIZE):
            chunk = sorted_feeds[start : start + HYDRATION_CHUNK_SIZE]
            feed_rows = (
                (
                    await session.execute(
                        sa.select(Feed)
                        .where(Feed.id.in_(chunk))
                        .order_by(Feed.id.asc())
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for f in feed_rows:
                feeds_by_id[f.id] = f

    # 3c. Items union locked in strict id ASC order (lightweight PK lock if scoped bulk present)
    scoped_candidate_ids: set[uuid.UUID] = set()
    for idx in scoped_bulk_indices:
        bm = cast(ItemMarkBulkReadMutation, mutations[idx])
        if bm.scope == "feed" and bm.feed_id not in feeds_by_id:
            continue
        if bm.scope == "category" and bm.category_id not in active_category_ids:
            continue
        cand_ids = (
            (await session.execute(sa.select(Item.id).where(*_scoped_bulk_conditions(bm))))
            .scalars()
            .all()
        )
        scoped_candidate_ids.update(cand_ids)

    all_item_ids_to_lock = sorted(explicit_item_ids | scoped_candidate_ids)
    if scoped_candidate_ids:
        # Lock union of explicit + scoped candidate IDs in strict id ASC order without TOAST bloat
        for start in range(0, len(all_item_ids_to_lock), LOCK_CHUNK_SIZE):
            chunk = all_item_ids_to_lock[start : start + LOCK_CHUNK_SIZE]
            await session.execute(
                sa.text(
                    "SELECT id FROM items WHERE id = ANY(CAST(:ids AS uuid[])) "
                    "ORDER BY id ASC FOR UPDATE"
                ),
                {"ids": chunk},
            )

    items_by_id: dict[uuid.UUID, Item] = {}
    if explicit_item_ids:
        sorted_explicit = sorted(explicit_item_ids)
        for start in range(0, len(sorted_explicit), HYDRATION_CHUNK_SIZE):
            chunk = sorted_explicit[start : start + HYDRATION_CHUNK_SIZE]
            stmt = sa.select(Item).where(Item.id.in_(chunk)).order_by(Item.id.asc())
            if not scoped_candidate_ids:
                stmt = stmt.with_for_update()
            item_rows = (await session.execute(stmt)).scalars().all()
            for it in item_rows:
                items_by_id[it.id] = it

    # Step 4: Process mutations sequentially in request array order
    seen_in_batch: dict[uuid.UUID, tuple[str, SyncPushResult]] = {}
    results: list[SyncPushResult] = []
    staged_mutations: list[dict[str, Any]] = []
    staged_events: list[SyncEvent] = []
    poll_feed_enqueues: list[uuid.UUID] = []

    for idx, mutation in enumerate(mutations):
        mid = mutation.client_mutation_id
        payload_hash = payload_hashes[idx]

        # 4a. Duplicate inside same request array
        if mid in seen_in_batch:
            prev_hash, prev_result = seen_in_batch[mid]
            if prev_hash == payload_hash:
                results.append(prev_result)
            else:
                results.append(
                    SyncPushResult(
                        client_mutation_id=mid,
                        outcome="rejected",
                        reason="mutation_id_conflict",
                    )
                )
            continue

        # 4b. Already persisted in sync_mutations
        persisted = existing_mutations.get(mid)
        if persisted is not None:
            if persisted.device_id != device_id or persisted.payload_sha256 != payload_hash:
                res = SyncPushResult(
                    client_mutation_id=mid,
                    outcome="rejected",
                    reason="mutation_id_conflict",
                )
            else:
                ent_type, ent_snap = await _build_replay_snapshot(
                    session,
                    persisted,
                    items_by_id=items_by_id,
                    feeds_by_id=feeds_by_id,
                )
                res = SyncPushResult(
                    client_mutation_id=mid,
                    outcome=cast(Literal["applied", "superseded", "rejected"], persisted.outcome),
                    reason=persisted.rejection_reason,
                    affected_count=persisted.affected_count,
                    current_entity_type=ent_type,
                    current_entity=ent_snap,
                )
            seen_in_batch[mid] = (payload_hash, res)
            results.append(res)
            continue

        # 4c. New mutation execution
        entity_type, entity_id = _mutation_entity_meta(mutation)
        effective_ts, skew_reason = clamp_client_timestamp(mutation.client_timestamp, server_now)

        if isinstance(mutation, ReservedFutureMutation):
            outcome: Literal["applied", "superseded", "rejected"] = "rejected"
            reason: str | None = "unsupported_mutation_type"
            affected_count: int | None = None
            cur_type: Literal["item", "feed"] | None = None
            cur_entity: SyncPushItemSnapshot | SyncPushFeedSnapshot | None = None
            eff_to_store = effective_ts or server_now
        elif skew_reason is not None or effective_ts is None:
            outcome = "rejected"
            reason = "clock_skew"
            affected_count = None
            eff_to_store = server_now
            cur_type, cur_entity = _snapshot_for_target(
                mutation, items_by_id=items_by_id, feeds_by_id=feeds_by_id
            )
        elif isinstance(mutation, ItemSetStateMutation):
            eff_to_store = effective_ts
            affected_count = None
            item = items_by_id.get(mutation.item_id)
            if item is None:
                outcome = "rejected"
                reason = "entity_not_found"
                cur_type, cur_entity = None, None
            else:
                snap = ItemStateSnapshot(
                    state=item.state,
                    read_at=item.read_at,
                    expired_at=item.expired_at,
                    unexpired_at=item.unexpired_at,
                    read_state_updated_at=item.read_state_updated_at,
                )
                merge_out, next_snap = merge_item_state(
                    snap, new_state=mutation.state, effective_ts=effective_ts
                )
                if merge_out == "applied":
                    item.state = next_snap.state
                    item.read_at = next_snap.read_at
                    item.unexpired_at = next_snap.unexpired_at
                    item.read_state_updated_at = next_snap.read_state_updated_at
                    item.updated_at = server_now
                    staged_events.append(SyncEvent("item", item.id, "upsert"))
                    outcome = "applied"
                    reason = None
                    cur_type, cur_entity = None, None
                else:
                    outcome = "superseded"
                    reason = None
                    cur_type = "item"
                    cur_entity = SyncPushItemSnapshot.model_validate(item)
        elif isinstance(mutation, ItemSetStarredMutation):
            eff_to_store = effective_ts
            affected_count = None
            item = items_by_id.get(mutation.item_id)
            if item is None:
                outcome = "rejected"
                reason = "entity_not_found"
                cur_type, cur_entity = None, None
            else:
                star_snap = ItemStarSnapshot(
                    is_starred=item.is_starred,
                    starred_at=item.starred_at,
                    starred_updated_at=item.starred_updated_at,
                )
                merge_out, next_star = merge_item_starred(
                    star_snap, new_starred=mutation.is_starred, effective_ts=effective_ts
                )
                if merge_out == "applied":
                    item.is_starred = next_star.is_starred
                    item.starred_at = next_star.starred_at
                    item.starred_updated_at = next_star.starred_updated_at
                    item.updated_at = server_now
                    staged_events.append(SyncEvent("item", item.id, "upsert"))
                    outcome = "applied"
                    reason = None
                    cur_type, cur_entity = None, None
                else:
                    outcome = "superseded"
                    reason = None
                    cur_type = "item"
                    cur_entity = SyncPushItemSnapshot.model_validate(item)
        elif isinstance(mutation, FeedSetFieldMutation):
            eff_to_store = effective_ts
            affected_count = None
            feed = feeds_by_id.get(mutation.feed_id)
            if feed is None:
                outcome = "rejected"
                reason = "entity_not_found"
                cur_type, cur_entity = None, None
            else:
                ok, coerced_val, val_err = _validate_feed_field_value(
                    mutation.field, mutation.value, active_category_ids
                )
                if not ok:
                    outcome = "rejected"
                    reason = val_err
                    cur_type = "feed"
                    cur_entity = SyncPushFeedSnapshot.model_validate(feed)
                else:
                    cur_val = _get_feed_field_value(feed, mutation.field)
                    stored_ts = parse_field_timestamp(
                        feed.field_updated_at, mutation.field, feed.created_at
                    )
                    merge_out, winning_val, winning_ts = merge_feed_field(
                        field=mutation.field,
                        current_value=cur_val,
                        stored_ts=stored_ts,
                        new_value=coerced_val,
                        effective_ts=effective_ts,
                    )
                    if merge_out == "applied":
                        should_poll = _apply_feed_field_value(
                            feed,
                            field=mutation.field,
                            value=winning_val,
                            ts=winning_ts,
                            server_now=server_now,
                        )
                        if should_poll:
                            poll_feed_enqueues.append(feed.id)
                        staged_events.append(SyncEvent("feed", feed.id, "upsert"))
                        outcome = "applied"
                        reason = None
                        cur_type, cur_entity = None, None
                    else:
                        outcome = "superseded"
                        reason = None
                        cur_type = "feed"
                        cur_entity = SyncPushFeedSnapshot.model_validate(feed)
        else:
            # ItemMarkBulkReadMutation
            eff_to_store = effective_ts
            outcome, reason, affected_count, cur_type, cur_entity = await _apply_bulk_mark_read(
                session,
                mutation=mutation,
                effective_ts=effective_ts,
                server_now=server_now,
                current_epoch=current_epoch,
                active_category_ids=active_category_ids,
                feeds_by_id=feeds_by_id,
                items_by_id=items_by_id,
                staged_events=staged_events,
            )

        res = SyncPushResult(
            client_mutation_id=mid,
            outcome=outcome,
            reason=reason,
            affected_count=affected_count,
            current_entity_type=cur_type,
            current_entity=cur_entity,
        )
        seen_in_batch[mid] = (payload_hash, res)
        results.append(res)
        staged_mutations.append(
            {
                "client_mutation_id": mid,
                "device_id": device_id,
                "mutation_type": mutation.type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "payload_sha256": payload_hash,
                "client_timestamp": mutation.client_timestamp,
                "effective_timestamp": eff_to_store,
                "server_applied_at": server_now,
                "outcome": outcome,
                "rejection_reason": reason,
                "affected_count": affected_count,
            }
        )

    # Flush ORM changes before jobs/sync_mutations/sync_log
    await session.flush()

    # Step 5: Execute staged enqueue_job() calls (jobs lock after items, before sync_state; §5.1)
    for fid in dict.fromkeys(poll_feed_enqueues):
        await enqueue_job(
            session,
            job_type="poll_feed",
            dedupe_key=f"feed:{fid}",
            feed_id=fid,
            priority=10,
            bump_existing=True,
        )

    # Step 6: Bulk-insert staged SyncMutation rows (chunked at 500 for D1 safety)
    for start in range(0, len(staged_mutations), HYDRATION_CHUNK_SIZE):
        await session.execute(
            sa.insert(SyncMutation),
            staged_mutations[start : start + HYDRATION_CHUNK_SIZE],
        )

    # Step 7: Coalesce staged SyncEvents preserving order and call sync_log.record() ONCE
    if staged_events:
        deduped_events = list(dict.fromkeys(staged_events))
        await sync_log.record(session, deduped_events)

    # Step 8: Commit and return SyncPushResponse (no watermark!)
    await session.commit()
    return SyncPushResponse(
        instance_epoch=current_epoch,
        server_now=server_now,
        results=results,
    )


def _snapshot_for_target(
    mutation: SyncMutationInput,
    *,
    items_by_id: dict[uuid.UUID, Item],
    feeds_by_id: dict[uuid.UUID, Feed],
) -> tuple[Literal["item", "feed"] | None, SyncPushItemSnapshot | SyncPushFeedSnapshot | None]:
    if isinstance(mutation, (ItemSetStateMutation, ItemSetStarredMutation)):
        item = items_by_id.get(mutation.item_id)
        if item is not None:
            return "item", SyncPushItemSnapshot.model_validate(item)
    elif isinstance(mutation, FeedSetFieldMutation):
        feed = feeds_by_id.get(mutation.feed_id)
        if feed is not None:
            return "feed", SyncPushFeedSnapshot.model_validate(feed)
    return None, None


def _get_feed_field_value(feed: Feed, field: str) -> Any:
    if field == "status":
        return feed.status
    if field == "category_id":
        return feed.category_id
    if field == "expiration_days_override":
        return feed.expiration_days_override
    rp = feed.research_policy or {}
    if field == "research_policy.enabled":
        return bool(rp.get("enabled", False))
    if field == "research_policy.fetch_pdfs":
        return bool(rp.get("fetch_pdfs", False))
    raise ValueError(f"Unsupported field: {field}")


def _apply_feed_field_value(
    feed: Feed,
    *,
    field: str,
    value: Any,
    ts: datetime,
    server_now: datetime,
) -> bool:
    should_poll = False
    if field == "status":
        should_poll = apply_feed_status_transition(feed, str(value), server_now)
    elif field == "category_id":
        feed.category_id = value
    elif field == "expiration_days_override":
        feed.expiration_days_override = value
    elif field in ("research_policy.enabled", "research_policy.fetch_pdfs"):
        subkey = field.split(".", 1)[1]
        rp = dict(feed.research_policy or {})
        rp[subkey] = bool(value)
        feed.research_policy = rp

    updated_ts = dict(feed.field_updated_at or {})
    updated_ts[field] = ts.isoformat()
    feed.field_updated_at = updated_ts
    feed.updated_at = server_now
    return should_poll


async def _apply_bulk_mark_read(
    session: AsyncSession,
    *,
    mutation: ItemMarkBulkReadMutation,
    effective_ts: datetime,
    server_now: datetime,
    current_epoch: uuid.UUID,
    active_category_ids: set[uuid.UUID],
    feeds_by_id: dict[uuid.UUID, Feed],
    items_by_id: dict[uuid.UUID, Item],
    staged_events: list[SyncEvent],
) -> tuple[
    Literal["applied", "superseded", "rejected"],
    str | None,
    int | None,
    Literal["item", "feed"] | None,
    SyncPushItemSnapshot | SyncPushFeedSnapshot | None,
]:
    if mutation.scope == "items":
        # True per-item LWW over state IN ('unread', 'read'); expired excluded (Q-2, RR-2)
        affected = 0
        for iid in dict.fromkeys(mutation.item_ids or ()):
            item = items_by_id.get(iid)
            if item is None or item.state not in ("unread", "read"):
                continue
            snap = ItemStateSnapshot(
                state=item.state,
                read_at=item.read_at,
                expired_at=item.expired_at,
                unexpired_at=item.unexpired_at,
                read_state_updated_at=item.read_state_updated_at,
            )
            merge_out, next_snap = merge_item_state(
                snap, new_state="read", effective_ts=effective_ts
            )
            if merge_out == "applied":
                item.state = next_snap.state
                item.read_at = next_snap.read_at
                item.unexpired_at = next_snap.unexpired_at
                item.read_state_updated_at = next_snap.read_state_updated_at
                item.updated_at = server_now
                staged_events.append(SyncEvent("item", item.id, "upsert"))
                affected += 1
        return "applied", None, affected, None, None

    # Scoped modes ('all', 'feed', 'category'): epoch guard first (P-11-F1)
    if mutation.epoch != current_epoch:
        return "rejected", "epoch_mismatch", None, None, None

    if mutation.scope == "feed":
        if mutation.feed_id not in feeds_by_id:
            return "rejected", "entity_not_found", None, None, None
    elif mutation.scope == "category" and mutation.category_id not in active_category_ids:
        return "rejected", "category_not_found", None, None, None

    # Flush any prior in-batch ORM mutations so Core UPDATE sees exact current item states
    if items_by_id:
        await session.flush()

    update_stmt = (
        sa.update(Item)
        .where(*_scoped_bulk_conditions(mutation, effective_ts=effective_ts))
        .values(
            state="read",
            read_at=effective_ts,
            read_state_updated_at=effective_ts,
            unexpired_at=None,
            updated_at=server_now,
        )
        .returning(Item.id)
    )
    updated_ids: list[uuid.UUID] = []

    def _collect_bulk_events(res: Any) -> list[SyncEvent]:
        ids = list(res.scalars().all())
        updated_ids.extend(ids)
        return [SyncEvent("item", iid, "upsert") for iid in ids]

    _, bulk_events = await execute_synced(session, update_stmt, _collect_bulk_events)
    staged_events.extend(bulk_events)

    # Keep any overlapping ORM-cached items in sync with the Core SQL UPDATE
    for iid in updated_ids:
        cached = items_by_id.get(iid)
        if cached is not None:
            session.expire(cached)
            refreshed = await session.get(Item, iid)
            if refreshed is not None:
                items_by_id[iid] = refreshed

    return "applied", None, len(updated_ids), None, None


async def sync_push(
    session: AsyncSession,
    *,
    device_id: uuid.UUID,
    request: SyncPushRequest,
    max_attempts: int = MAX_PUSH_RETRIES,
) -> SyncPushResponse:
    """Execute POST /api/v1/sync/push with bounded server-side transient-error retries (§6.7)."""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await _execute_push_once(session, device_id=device_id, request=request)
        except (OperationalError, IntegrityError, DBAPIError) as exc:
            await session.rollback()
            if not _is_retryable_push_error(exc):
                raise
            last_exc = exc
            if attempt < max_attempts:
                await asyncio.sleep(random.uniform(RETRY_BACKOFF_MIN_S, RETRY_BACKOFF_MAX_S))  # noqa: S311

    raise PushTransientError(
        f"sync/push exhausted {max_attempts} retry attempts after transient DB error: {last_exc}"
    ) from last_exc


def coalesce_sync_events(events: Sequence[SyncEvent]) -> list[SyncEvent]:
    """Deduplicate SyncEvents preserving first-seen order."""
    return list(dict.fromkeys(events))
