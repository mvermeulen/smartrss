"""Pydantic v2 sync wire protocol schemas (bootstrap, pull, push, slim snapshots; §6)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_PUSH_MUTATIONS = 500
MAX_BULK_ITEM_IDS_PER_MUTATION = 1000
MAX_TOTAL_EXPLICIT_ITEM_IDS_PER_PUSH = 5000
MAX_PUSH_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB (P-09)


def _ensure_aware(dt: datetime, field_name: str) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return dt


class SyncCategoryDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    sort_order: int
    updated_at: datetime


class SyncFeedDTO(BaseModel):
    """Canonical synced feed DTO (omits online-only last_fetched_at/last_success_at; P-06)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category_id: uuid.UUID | None
    feed_url: str
    site_url: str | None
    title: str
    status: str
    health_state: str
    poll_interval_minutes: int
    consecutive_errors: int
    last_error: str | None
    expiration_days_override: int | None
    fetch_full_text: bool
    research_policy: dict[str, Any]
    curation_instructions: str
    active_feed_instructions: list[Any] = Field(default_factory=list)
    config_version: int
    field_updated_at: dict[str, str]
    updated_at: datetime


class SyncItemDTO(BaseModel):
    """Metadata-only forward-compatible item aggregate (1b-D02, 1b-D06)."""

    id: uuid.UUID
    feed_id: uuid.UUID
    cluster_id: uuid.UUID | None = None
    raw_url: str
    canonical_url: str
    title: str
    author: str | None
    published_at: datetime | None
    fetched_at: datetime
    raw_summary: str | None
    summary_truncated: bool = False
    lead_excerpt: str | None = None
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
    enrichment: Any | None = None
    tags: list[str] = Field(default_factory=list)
    research_sources: list[Any] = Field(default_factory=list)
    user_feedback: Any | None = None
    updated_at: datetime


class SyncAppSettingsDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    default_expiration_days: int
    backfill_cap: int
    default_poll_interval_minutes: int
    updated_at: datetime


class SyncBootstrapResponse(BaseModel):
    instance_epoch: uuid.UUID
    server_now: datetime
    watermark: int | None
    categories: list[SyncCategoryDTO] | None
    feeds: list[SyncFeedDTO] | None
    clusters: list[Any] | None
    interest_profile: Any | None
    app_settings: SyncAppSettingsDTO | None
    items: list[SyncItemDTO]
    next_cursor: uuid.UUID | None
    has_more: bool


SyncChangeData = SyncCategoryDTO | SyncFeedDTO | SyncItemDTO | SyncAppSettingsDTO | None


class SyncPullChange(BaseModel):
    seq: int
    entity_type: str
    entity_id: uuid.UUID
    op: Literal["upsert", "delete"]
    changed_at: datetime
    data: SyncChangeData


class SyncPullResponse(BaseModel):
    resync_required: bool
    resync_reason: Literal["epoch_mismatch", "cursor_ahead", "cursor_pruned"] | None
    instance_epoch: uuid.UUID
    server_now: datetime
    watermark: int
    next_seq: int
    has_more: bool
    changes: list[SyncPullChange]


class _BaseMutation(BaseModel):
    client_mutation_id: uuid.UUID
    client_timestamp: datetime

    @field_validator("client_timestamp")
    @classmethod
    def _validate_client_timestamp(cls, value: datetime) -> datetime:
        return _ensure_aware(value, "client_timestamp")


class ItemSetStateMutation(_BaseMutation):
    type: Literal["item_set_state"]
    item_id: uuid.UUID
    state: Literal["read", "unread"]


class ItemSetStarredMutation(_BaseMutation):
    type: Literal["item_set_starred"]
    item_id: uuid.UUID
    is_starred: bool


class ItemMarkBulkReadMutation(_BaseMutation):
    type: Literal["item_mark_bulk_read"]
    scope: Literal["all", "feed", "category", "items"]
    up_to_sync_seq: int | None = Field(default=None, ge=0)
    epoch: uuid.UUID | None = None
    feed_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    older_than_ts: datetime | None = None
    item_ids: list[uuid.UUID] | None = None

    @field_validator("older_than_ts")
    @classmethod
    def _validate_older_than(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            return _ensure_aware(value, "older_than_ts")
        return None

    @model_validator(mode="after")
    def _validate_scope_fields(self) -> ItemMarkBulkReadMutation:
        if self.scope == "all":
            if self.up_to_sync_seq is None or self.epoch is None:
                raise ValueError("scope='all' requires up_to_sync_seq and epoch")
            if any(val is not None for val in (self.feed_id, self.category_id, self.item_ids)):
                raise ValueError("scope='all' forbids feed_id, category_id, and item_ids")
        elif self.scope == "feed":
            if self.feed_id is None or self.up_to_sync_seq is None or self.epoch is None:
                raise ValueError("scope='feed' requires feed_id, up_to_sync_seq, and epoch")
            if self.category_id is not None or self.item_ids is not None:
                raise ValueError("scope='feed' forbids category_id and item_ids")
        elif self.scope == "category":
            if self.category_id is None or self.up_to_sync_seq is None or self.epoch is None:
                raise ValueError("scope='category' requires category_id, up_to_sync_seq, and epoch")
            if self.feed_id is not None or self.item_ids is not None:
                raise ValueError("scope='category' forbids feed_id and item_ids")
        elif self.scope == "items":
            if (
                self.item_ids is None
                or len(self.item_ids) == 0
                or len(self.item_ids) > MAX_BULK_ITEM_IDS_PER_MUTATION
            ):
                raise ValueError(
                    f"scope='items' requires 1..{MAX_BULK_ITEM_IDS_PER_MUTATION} item_ids"
                )
            if self.feed_id is not None or self.category_id is not None:
                raise ValueError("scope='items' forbids feed_id and category_id")
        return self


class FeedSetFieldMutation(_BaseMutation):
    type: Literal["feed_set_field"]
    feed_id: uuid.UUID
    field: Literal[
        "status",
        "category_id",
        "expiration_days_override",
        "research_policy.enabled",
        "research_policy.fetch_pdfs",
    ]
    value: Any


class ReservedFutureMutation(_BaseMutation):
    """Reserved future milestone mutation types rejected gracefully per §6.4."""

    model_config = ConfigDict(extra="allow")

    type: Literal[
        "split_cluster",
        "item_feedback",
        "feed_instruction_add",
        "feed_instruction_archive",
    ]


SyncMutationInput = Annotated[
    ItemSetStateMutation
    | ItemSetStarredMutation
    | ItemMarkBulkReadMutation
    | FeedSetFieldMutation
    | ReservedFutureMutation,
    Field(discriminator="type"),
]


class SyncPushRequest(BaseModel):
    mutations: list[SyncMutationInput] = Field(max_length=MAX_PUSH_MUTATIONS)

    @model_validator(mode="after")
    def _validate_total_explicit_item_ids(self) -> SyncPushRequest:
        total_ids = sum(
            len(m.item_ids)
            for m in self.mutations
            if isinstance(m, ItemMarkBulkReadMutation)
            and m.scope == "items"
            and m.item_ids is not None
        )
        if total_ids > MAX_TOTAL_EXPLICIT_ITEM_IDS_PER_PUSH:
            raise ValueError(
                f"Total explicit item_ids per push must not exceed "
                f"{MAX_TOTAL_EXPLICIT_ITEM_IDS_PER_PUSH} (got {total_ids})"
            )
        return self


class SyncPushItemSnapshot(BaseModel):
    """Slim mutable-field snapshot returned on superseded/rejected item mutations (P-08)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    state: str
    is_starred: bool
    read_at: datetime | None
    starred_at: datetime | None
    expired_at: datetime | None
    unexpired_at: datetime | None
    read_state_updated_at: datetime
    starred_updated_at: datetime
    updated_at: datetime


class SyncPushFeedSnapshot(BaseModel):
    """Slim mutable-field snapshot returned on superseded/rejected feed mutations (P-08)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    category_id: uuid.UUID | None
    expiration_days_override: int | None
    research_policy: dict[str, Any]
    config_version: int
    field_updated_at: dict[str, str]
    updated_at: datetime


class SyncPushResult(BaseModel):
    client_mutation_id: uuid.UUID
    outcome: Literal["applied", "superseded", "rejected"]
    reason: str | None = None
    affected_count: int | None = None
    current_entity_type: Literal["item", "feed"] | None = None
    current_entity: SyncPushItemSnapshot | SyncPushFeedSnapshot | None = None


class SyncPushResponse(BaseModel):
    """Push response deliberately omits watermark so pull cursor advances only from pull."""

    instance_epoch: uuid.UUID
    server_now: datetime
    results: list[SyncPushResult]
