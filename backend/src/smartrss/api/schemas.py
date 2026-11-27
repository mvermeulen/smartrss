"""Pydantic v2 request and response schemas for Milestone 1a REST API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db: Literal["ok", "error"]
    version: str


class PairExchangeRequest(BaseModel):
    code: str = Field(min_length=4, max_length=64)
    device_name: str | None = Field(default=None, min_length=1, max_length=120)
    platform: Literal["ubuntu", "android", "web"] | None = None


class PairExchangeResponse(BaseModel):
    device_id: uuid.UUID
    device_name: str
    platform: Literal["ubuntu", "android", "web"]
    bearer_token: str


class CreatePairingCodeRequest(BaseModel):
    device_name: str = Field(min_length=1, max_length=120)
    platform: Literal["ubuntu", "android", "web"] = "ubuntu"


class CreatePairingCodeResponse(BaseModel):
    code: str
    expires_at: datetime


class DeviceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    platform: str
    created_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None


class CategoryCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    sort_order: int = 0


class CategoryUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    sort_order: int | None = None


class CategoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    sort_order: int
    created_at: datetime
    updated_at: datetime


class ResearchPolicySchema(BaseModel):
    enabled: bool = False
    fetch_pdfs: bool = False
    guidance: str = ""
    max_hops: int = Field(default=1, ge=1, le=3)


class FeedCreateRequest(BaseModel):
    feed_url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=300)
    category_id: uuid.UUID | None = None
    poll_interval_minutes: int | None = Field(default=None, ge=5, le=10080)
    expiration_days_override: int | None = Field(default=None, ge=0, le=3650)
    fetch_full_text: bool = True
    research_policy: ResearchPolicySchema | None = None
    curation_instructions: str = ""


class FeedUpdateRequest(BaseModel):
    feed_url: str | None = Field(default=None, min_length=1, max_length=2048)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    status: Literal["active", "paused", "muted", "unsubscribed"] | None = None
    category_id: uuid.UUID | None = None
    poll_interval_minutes: int | None = Field(default=None, ge=5, le=10080)
    expiration_days_override: int | None = Field(default=None, ge=0, le=3650)
    fetch_full_text: bool | None = None
    research_policy: ResearchPolicySchema | None = None
    curation_instructions: str | None = None
    expected_config_version: int | None = None


class FeedResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category_id: uuid.UUID | None
    feed_url: str
    site_url: str | None
    title: str
    status: str
    health_state: str
    poll_interval_minutes: int
    last_fetched_at: datetime | None
    last_success_at: datetime | None
    next_fetch_at: datetime | None
    consecutive_errors: int
    last_error: str | None
    initial_backfill_done: bool
    expiration_days_override: int | None
    fetch_full_text: bool
    research_policy: dict[str, Any]
    curation_instructions: str
    field_updated_at: dict[str, str]
    config_version: int
    created_at: datetime
    updated_at: datetime


class FeedPollTriggerResponse(BaseModel):
    feed_id: uuid.UUID
    job_id: int | None
    queued: bool


class OpmlImportRequest(BaseModel):
    opml_xml: str = Field(min_length=1)
    default_category_id: uuid.UUID | None = None


class OpmlImportResponse(BaseModel):
    imported: int
    reactivated: int
    skipped_duplicates: int
    rejected_invalid: int


class ItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    feed_id: uuid.UUID
    guid: str
    raw_url: str
    canonical_url: str
    title: str
    author: str | None
    published_at: datetime | None
    fetched_at: datetime
    raw_summary: str | None
    extracted_text: str | None
    extraction_status: str
    entry_hash: str | None
    content_hash: str | None
    state: str
    is_starred: bool
    read_at: datetime | None
    starred_at: datetime | None
    expired_at: datetime | None
    read_state_updated_at: datetime
    starred_updated_at: datetime
    unexpired_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ItemListResponse(BaseModel):
    items: list[ItemResponse]
    total: int
    limit: int
    offset: int


class PipelineRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    trigger: str
    stages: list[str]
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    feeds_polled: int = 0
    feeds_not_modified: int = 0
    items_polled: int
    items_extracted: int
    items_clustered: int
    items_researched: int
    items_enriched: int
    jobs_failed: int
    total_llm_ms: int
    notes: str | None


class DeadLetterJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_type: str
    dedupe_key: str
    payload: dict[str, Any]
    feed_id: uuid.UUID | None
    item_id: uuid.UUID | None
    run_id: uuid.UUID | None
    priority: int
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None
    finished_at: datetime | None
    created_at: datetime


class DeadLetterListResponse(BaseModel):
    jobs: list[DeadLetterJobResponse]
    total: int
    limit: int
    offset: int


class SystemHealthResponse(BaseModel):
    db: Literal["ok", "error"]
    instance_epoch: uuid.UUID
    current_seq: int
    queue_counts: dict[str, dict[str, int]]
    dead_letter_count: int
    recent_runs: list[PipelineRunResponse]


class AppSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    default_expiration_days: int
    backfill_cap: int
    default_poll_interval_minutes: int
    updated_at: datetime


class AppSettingsUpdateRequest(BaseModel):
    default_expiration_days: int | None = Field(default=None, ge=0, le=3650)
    backfill_cap: int | None = Field(default=None, ge=0, le=500)
    default_poll_interval_minutes: int | None = Field(default=None, ge=5, le=10080)

    @model_validator(mode="after")
    def _reject_explicit_nulls(self) -> AppSettingsUpdateRequest:
        for field_name in self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class SnippetFragmentDTO(BaseModel):
    text: str
    highlight: bool


class SearchItemDTO(BaseModel):
    """Slim archive search item DTO omitting raw_summary/summary_truncated (P-08)."""

    id: uuid.UUID
    feed_id: uuid.UUID
    cluster_id: uuid.UUID | None = None
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
    enrichment: Any | None = None
    tags: list[str] = Field(default_factory=list)
    research_sources: list[Any] = Field(default_factory=list)
    user_feedback: Any | None = None
    updated_at: datetime


class SearchResultDTO(BaseModel):
    item: SearchItemDTO
    rank: float | None
    snippet: list[list[SnippetFragmentDTO]]


class SearchResponse(BaseModel):
    query: str | None
    sort: Literal["relevance", "newest"]
    total: int
    limit: int
    offset: int
    results: list[SearchResultDTO]
