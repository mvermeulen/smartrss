"""SQLAlchemy 2.0 ORM models for Milestone 1a."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DEFAULT_RESEARCH_POLICY: dict[str, Any] = {
    "enabled": False,
    "fetch_pdfs": False,
    "guidance": "",
    "max_hops": 1,
}


class Base(DeclarativeBase):
    """Declarative base for SmartRSS ORM models."""


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    platform: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    token_hash: Mapped[str] = mapped_column(sa.Text(), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class PairingCode(Base):
    __tablename__ = "pairing_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code_hash: Mapped[str] = mapped_column(sa.Text(), nullable=False, unique=True)
    device_name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    platform: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="ubuntu")
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_by_device_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("devices.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


class SyncState(Base):
    __tablename__ = "sync_state"

    id: Mapped[int] = mapped_column(sa.SmallInteger(), primary_key=True)
    current_seq: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False, default=0)
    instance_epoch: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


class SyncChange(Base):
    __tablename__ = "sync_changes"

    seq: Mapped[int] = mapped_column(sa.BigInteger(), primary_key=True, autoincrement=False)
    entity_type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    op: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    sort_order: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class Feed(Base):
    __tablename__ = "feeds"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    feed_url: Mapped[str] = mapped_column(sa.Text(), nullable=False, unique=True)
    site_url: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    title: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="active")
    health_state: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="healthy")
    poll_interval_minutes: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=60)
    etag: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    last_fetched_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    next_fetch_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    consecutive_errors: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    initial_backfill_done: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, default=False)
    expiration_days_override: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    fetch_full_text: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, default=True)
    research_policy: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        default=lambda: dict(DEFAULT_RESEARCH_POLICY),
    )
    curation_instructions: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="")
    field_updated_at: Mapped[dict[str, str]] = mapped_column(
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        default=dict,
    )
    config_version: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


class Item(Base):
    __tablename__ = "items"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    feed_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), sa.ForeignKey("feeds.id"), nullable=False
    )
    guid: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    raw_url: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    canonical_url: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    title: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    author: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    raw_summary: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    extraction_status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="pending")
    entry_hash: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    state: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="unread")
    is_starred: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, default=False)
    read_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    starred_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    read_state_updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    starred_updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    unexpired_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    trigger: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    stages: Mapped[list[str]] = mapped_column(postgresql.ARRAY(sa.Text()), nullable=False)
    status: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    items_polled: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    items_extracted: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    items_clustered: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    items_researched: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    items_enriched: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    jobs_failed: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    total_llm_ms: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(sa.BigInteger(), primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB(astext_type=sa.Text()), nullable=False, default=dict
    )
    feed_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("feeds.id", ondelete="SET NULL"),
        nullable=True,
    )
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("items.id", ondelete="SET NULL"),
        nullable=True,
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("pipeline_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    priority: Mapped[int] = mapped_column(sa.SmallInteger(), nullable=False, default=50)
    status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=5)
    run_after: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    locked_by: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    queue_wait_ms: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


class StageMetric(Base):
    __tablename__ = "stage_metrics"

    id: Mapped[int] = mapped_column(sa.BigInteger(), primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("pipeline_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    job_type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    feed_id: Mapped[uuid.UUID | None] = mapped_column(postgresql.UUID(as_uuid=True), nullable=True)
    item_id: Mapped[uuid.UUID | None] = mapped_column(postgresql.UUID(as_uuid=True), nullable=True)
    model_name: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    prompt_config_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=True
    )
    status: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    queue_wait_ms: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    duration_ms: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    llm_prompt_ms: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    llm_gen_ms: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    readiness_lag_ms: Mapped[int | None] = mapped_column(sa.BigInteger(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


class AppSettings(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(sa.SmallInteger(), primary_key=True)
    default_expiration_days: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=30)
    backfill_cap: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=10)
    default_poll_interval_minutes: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=60
    )
    last_retention_cleanup_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
